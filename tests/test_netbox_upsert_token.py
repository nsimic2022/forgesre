"""Unit tests for scripts/netbox-upsert-token.py. No live NetBox or Docker."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "netbox-upsert-token.py"
    spec = importlib.util.spec_from_file_location("netbox_upsert_token", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Field:
    def __init__(self, name: str, column: str | None = None):
        self.name = name
        self.column = column or name


class _Meta:
    db_table = "users_token"

    def __init__(self, names: list[str]):
        self.fields = [_Field(n) for n in names]

    def get_fields(self):
        return self.fields


class _QuerySet:
    def __init__(self, rows: list, store: list):
        self.rows = list(rows)
        self.store = store

    def filter(self, **kwargs):
        matched = []
        for row in self.store:
            ok = True
            for key, value in kwargs.items():
                if getattr(row, key, None) != value:
                    ok = False
                    break
            if ok:
                matched.append(row)
        return _QuerySet(matched, self.store)

    def order_by(self, *_args):
        return self

    def select_related(self, *_args):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)

    def exists(self):
        return bool(self.rows)

    def delete(self):
        n = 0
        for row in list(self.rows):
            if row in self.store:
                self.store.remove(row)
                n += 1
        return n, {}


class _Manager:
    def __init__(self, store: list):
        self.store = store

    def filter(self, **kwargs):
        return _QuerySet(self.store, self.store).filter(**kwargs)

    def create(self, **kwargs):
        token = kwargs.pop("token", None)
        row = FakeToken(token=token, **kwargs)
        row.save()
        return row


class FakeToken:
    store: list = []
    _meta = _Meta(
        ["user", "version", "plaintext", "key", "pepper_id", "hmac_digest", "write_enabled", "enabled", "description"]
    )
    objects = _Manager(store)

    def __init__(self, *args, token=None, **kwargs):
        self.pk = None
        self.id = None
        self.user = kwargs.get("user")
        self.user_id = getattr(self.user, "id", None)
        self.version = kwargs.get("version", 2)
        self.plaintext = kwargs.get("plaintext")
        self.key = kwargs.get("key")
        self.pepper_id = kwargs.get("pepper_id")
        self.hmac_digest = kwargs.get("hmac_digest")
        self.write_enabled = kwargs.get("write_enabled", True)
        self.enabled = kwargs.get("enabled", True)
        self.description = kwargs.get("description", "")
        if token is not None:
            if self.version == 1:
                self.plaintext = token
            else:
                raise ValueError("API_TOKEN_PEPPERS is not defined")

    def save(self):
        if self.plaintext and self.version != 1:
            raise ValueError("v2 cannot store plaintext")
        if self.version == 1 and not self.plaintext:
            raise ValueError("v1 requires plaintext")
        if self not in FakeToken.store:
            FakeToken.store.append(self)
        self.pk = FakeToken.store.index(self) + 1
        self.id = self.pk
        self.user_id = getattr(self.user, "id", None)

    def delete(self):
        if self in FakeToken.store:
            FakeToken.store.remove(self)


class PepperToken(FakeToken):
    """Constructor token= always hashes as v2 unless version was set — already on FakeToken."""


class NoTokenKwargToken(FakeToken):
    def __init__(self, *args, token=None, **kwargs):
        if token is not None and "plaintext" not in kwargs:
            raise TypeError("Token() got unexpected keyword argument 'token'")
        super().__init__(*args, token=None, **kwargs)


class FakeUser:
    store: list = []
    objects = _Manager(store)

    def __init__(self, username="admin", email="admin@forgesre.local", is_superuser=True, is_active=True):
        self.username = username
        self.email = email
        self.is_superuser = is_superuser
        self.is_active = is_active
        self.id = 1
        self.pk = 1
        self.user_permissions = _PermBag()

    def save(self):
        if self not in FakeUser.store:
            FakeUser.store.append(self)

    @classmethod
    def reset(cls, user=None):
        cls.store.clear()
        if user is not None:
            cls.store.append(user)


class _PermBag:
    def add(self, *_args):
        return None


class _UserManager(_Manager):
    def create_superuser(self, username, email, password):
        user = FakeUser(username=username, email=email, is_superuser=True)
        user.save()
        return user

    def filter(self, **kwargs):
        return _QuerySet(self.store, self.store).filter(**kwargs)


FakeUser.objects = _UserManager(FakeUser.store)


def setup_function():
    FakeToken.store.clear()
    FakeUser.store.clear()
    FakeToken.objects = _Manager(FakeToken.store)
    FakeUser.objects = _UserManager(FakeUser.store)


def test_looks_like_v2_token_helpers():
    mod = _load()
    assert mod.looks_like_v2_token("nbt_abcdefghijkl.notarealsecret") is True
    assert mod.looks_like_v2_token("Bearer nbt_abcdefghijkl.notarealsecret") is True
    assert mod.looks_like_v2_token("a" * 40) is False
    assert mod.looks_like_v2_token("") is False
    assert mod.looks_like_v2_public_key("AbCdEfGhIjKl") is True
    assert mod.looks_like_v2_public_key("nbt_abcdefghijkl.notarealsecret") is False
    assert mod.looks_like_v2_public_key("a" * 40) is False


def test_redact_strips_token():
    mod = _load()
    secret = "a" * 40
    assert secret not in mod.redact(f"IntegrityError: {secret} duplicate", secret)
    assert "[redacted]" in mod.redact(f"bad {secret}", secret)


def test_log_exc_includes_type_not_token(capsys):
    mod = _load()
    secret = "b" * 40
    try:
        raise ValueError(f"API_TOKEN_PEPPERS is not defined for {secret}")
    except ValueError as exc:
        mod.log_exc("v1 create constructor_token_kwarg failed", exc, secret)
    out = capsys.readouterr().out
    assert "ValueError" in out
    assert "API_TOKEN_PEPPERS is not defined" in out
    assert secret not in out
    assert "forgesre:" in out


def test_promote_user_skips_missing_is_staff():
    mod = _load()

    class User:
        username = "admin"
        is_active = True
        is_superuser = False

        def __getattr__(self, name):
            if name == "is_staff":
                raise AttributeError("is_staff")
            raise AttributeError(name)

        def save(self):
            self.saved = True

    user = User()
    mod.promote_user(user)
    assert user.is_superuser is True
    assert getattr(user, "saved", False) is True


def test_promote_user_does_not_set_is_staff():
    mod = _load()
    user = SimpleNamespace(username="admin", is_active=True, is_superuser=True)
    mod.promote_user(user)
    assert not hasattr(user, "is_staff")


def test_upsert_creates_v1_via_token_kwarg(capsys):
    mod = _load()
    secret = "c" * 40
    user = FakeUser()
    FakeUser.reset(user)
    status = mod.upsert_core_token(
        token_key=secret,
        username="admin",
        email="admin@forgesre.local",
        password="x",
        Token=FakeToken,
        User=FakeUser,
        v1=1,
    )
    assert status == "ready"
    assert len(FakeToken.store) == 1
    row = FakeToken.store[0]
    assert row.version == 1
    assert row.plaintext == secret
    assert row.write_enabled is False
    assert row.key is None
    out = capsys.readouterr().out
    assert "v1 token ready" in out
    assert secret not in out


def test_upsert_falls_back_when_token_kwarg_missing(capsys):
    mod = _load()
    secret = "d" * 40
    user = FakeUser()
    FakeUser.reset(user)
    NoTokenKwargToken.store = FakeToken.store
    NoTokenKwargToken.objects = FakeToken.objects
    NoTokenKwargToken._meta = FakeToken._meta
    status = mod.upsert_core_token(
        token_key=secret,
        username="admin",
        email="admin@forgesre.local",
        password="x",
        Token=NoTokenKwargToken,
        User=FakeUser,
        v1=1,
    )
    assert status == "ready"
    assert FakeToken.store[0].plaintext == secret
    out = capsys.readouterr().out
    assert "constructor_token_kwarg failed" in out
    assert "TypeError" in out
    assert "v1 token ready" in out
    assert secret not in out


def test_upsert_empty_token_is_honest_403_path(capsys):
    mod = _load()
    status = mod.upsert_core_token(
        token_key="",
        username="admin",
        email="admin@forgesre.local",
        password="x",
        Token=FakeToken,
        User=FakeUser,
        v1=1,
    )
    assert status == "empty"
    assert "HTTP 403" in capsys.readouterr().out


def test_upsert_skips_v2_token(capsys):
    mod = _load()
    secret = "nbt_abcdefghijkl.notarealsecret"
    user = FakeUser()
    FakeUser.reset(user)
    status = mod.upsert_core_token(
        token_key=secret,
        username="admin",
        email="admin@forgesre.local",
        password="x",
        Token=FakeToken,
        User=FakeUser,
        v1=1,
    )
    assert status == "v2"
    assert FakeToken.store == []
    out = capsys.readouterr().out
    assert "skipping v1 upsert" in out
    assert "v1 token ready" not in out
    assert secret not in out
    assert "nbt_abcdefghijkl" not in out


def test_upsert_skips_v2_token_with_bearer_prefix(capsys):
    mod = _load()
    secret = "nbt_abcdefghijkl.notarealsecret"
    status = mod.upsert_core_token(
        token_key=f"Bearer {secret}",
        username="admin",
        email="admin@forgesre.local",
        password="x",
        Token=FakeToken,
        User=FakeUser,
        v1=1,
    )
    assert status == "v2"
    assert FakeToken.store == []
    out = capsys.readouterr().out
    assert "skipping v1 upsert" in out
    assert secret not in out


def test_upsert_rejects_12_char_v2_key(capsys):
    mod = _load()
    key_only = "AbCdEfGhIjKl"
    try:
        mod.upsert_core_token(
            token_key=key_only,
            username="admin",
            email="admin@forgesre.local",
            password="x",
            Token=FakeToken,
            User=FakeUser,
            v1=1,
        )
    except mod.UpsertError as exc:
        assert "12-character" in str(exc)
        assert key_only not in str(exc)
    else:
        raise AssertionError("expected UpsertError")
    assert FakeToken.store == []
    out = capsys.readouterr().out
    assert "12-character" in out
    assert key_only not in out


def test_upsert_uses_existing_superuser_when_name_mismatches(capsys):
    mod = _load()
    secret = "e" * 40
    live = FakeUser(username="nsimic", email="n@lab.local", is_superuser=True)
    live.id = 7
    live.pk = 7
    FakeUser.reset(live)
    status = mod.upsert_core_token(
        token_key=secret,
        username="admin",
        email="admin@forgesre.local",
        password="",
        Token=FakeToken,
        User=FakeUser,
        v1=1,
    )
    assert status == "ready"
    assert FakeToken.store[0].user.username == "nsimic"
    out = capsys.readouterr().out
    assert "existing superuser nsimic" in out
    assert "v1 token ready" in out


def test_upsert_wrong_length_raises():
    mod = _load()
    try:
        mod.upsert_core_token(
            token_key="short",
            username="admin",
            email="admin@forgesre.local",
            password="x",
            Token=FakeToken,
            User=FakeUser,
            v1=1,
        )
    except mod.UpsertError as exc:
        assert "40" in str(exc)
    else:
        raise AssertionError("expected UpsertError")


def test_launch_and_compose_call_helper():
    launch = (ROOT / "scripts" / "netbox-launch.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    upsert = (ROOT / "scripts" / "netbox-upsert-token.py").read_text(encoding="utf-8")
    assert "forgesre-upsert-token.py" in launch
    assert "scripts/netbox-upsert-token.py:/opt/netbox/forgesre-upsert-token.py" in compose
    assert "if not user.is_staff" not in upsert
    assert "user.is_staff =" not in upsert
    assert "is_staff" in launch  # comment explaining why we must not touch it
    assert "if not user.is_staff" not in launch
    assert "user.is_staff =" not in launch
    assert "could not upsert NetBox API token (UI still starts)" in launch
    assert "token never landed" in launch
    assert "v1 token ready" in upsert
    assert "skipping v1 upsert" in upsert
    assert "looks_like_v2_token" in upsert
    assert "granian" in launch
    assert "exec granian" in launch
