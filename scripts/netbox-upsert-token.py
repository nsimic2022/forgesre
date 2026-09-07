#!/usr/bin/env python3
"""Attach NETBOX_API_TOKEN for Core sync. Prefer a NetBox UI v2 token.

Runs inside the bundled netboxcommunity/netbox:v4.6.x container (called from
scripts/netbox-launch.sh). Never prints the token value.

NetBox 4.6 UI issues v2 tokens (``nbt_<12-char key>.<secret>``, shown once).
If secrets already hold that full string, skip v1 create — do not overwrite
N's v2 token with a 40-char plaintext row. Core sends Authorization: Bearer.

If the secret is still a legacy 40-char v1 value, upsert v1 plaintext as
fallback (write_enabled=False, superuser + view permission). v1 is deprecated
in the NetBox UI; that is OK for this fallback.

NetBox 4.6 Token (users.models.Token / users.models.tokens.Token):
  version      1 = v1 plaintext (deprecated); 2 = v2 HMAC (model default)
  plaintext    CharField(max_length=40, unique, null) — v1 only
  key          CharField(max_length=12) — v2 public id, NOT the secret
  pepper_id, hmac_digest — v2 only; get_current_pepper() needs API_TOKEN_PEPPERS
CheckConstraint enforce_version_dependent_fields:
  v1: plaintext NOT NULL, key/pepper_id/hmac_digest NULL
  v2: plaintext NULL, key/pepper_id/hmac_digest NOT NULL
Token.__init__(..., token=<secret>) sets plaintext when version is already 1.
If version stays at the default 2, the setter hashes as v2 (or raises
ValueError: API_TOKEN_PEPPERS is not defined).

NetBox 4.5+ User has no is_staff (users.0013_user_remove_is_staff). Reading
user.is_staff raises AttributeError — that was swallowed by launch as
"could not upsert NetBox API token (UI still starts)" while Granian still
started, so Discovery stayed HTTP 403.

Do not import this module as a Django app. Tests load it with importlib.
"""

from __future__ import annotations

import os
import sys
import traceback

FORGESRE_PREFIX = "forgesre:"
CORE_DESCRIPTION = "ForgeSRE Core read-sync"
V1_LENGTH = 40
V2_PREFIX = "nbt_"
V2_PUBLIC_KEY_LENGTH = 12


class UpsertError(RuntimeError):
    """Operator-visible upsert failure (never includes the token)."""


def looks_like_v2_token(token_key: str) -> bool:
    """True for NetBox v2 secrets (typically nbt_<key>.<secret>). Never log the value."""
    value = (token_key or "").strip()
    lower = value.lower()
    if lower.startswith("bearer ") or lower.startswith("token "):
        parts = value.split(None, 1)
        value = parts[1].strip() if len(parts) == 2 else value
        lower = value.lower()
    return lower.startswith(V2_PREFIX)


def looks_like_v2_public_key(token_key: str) -> bool:
    """12-char key shown in the UI list — not the full secret shown once at create."""
    value = (token_key or "").strip()
    return len(value) == V2_PUBLIC_KEY_LENGTH and not looks_like_v2_token(value) and value.isalnum()


def redact(text: object, secret: str = "") -> str:
    """Stringify an error without leaking the API token."""
    out = "" if text is None else str(text)
    if secret:
        out = out.replace(secret, "[redacted]")
    return out


def log(message: str) -> None:
    print(f"{FORGESRE_PREFIX} {message}", flush=True)


def log_exc(prefix: str, exc: BaseException, secret: str = "") -> None:
    """Log exception type + message. Never the token. No traceback dump of locals."""
    name = type(exc).__name__
    msg = redact(exc, secret).replace("\n", " ").strip()
    if len(msg) > 400:
        msg = msg[:400] + "…"
    log(f"{prefix}: {name}: {msg}")


def token_field_names(Token) -> set[str]:
    names: set[str] = set()
    try:
        for field in Token._meta.get_fields():
            name = getattr(field, "name", None)
            if name:
                names.add(name)
    except Exception:
        names.update({"user", "version", "plaintext", "key", "pepper_id", "hmac_digest", "write_enabled", "enabled", "description"})
    return names


def promote_user(user) -> None:
    """Active superuser. Never touch is_staff — removed in NetBox 4.5."""
    dirty = False
    if not getattr(user, "is_active", True):
        user.is_active = True
        dirty = True
    if not getattr(user, "is_superuser", False):
        user.is_superuser = True
        dirty = True
        log(f"promoted {getattr(user, 'username', '?')} to superuser for Core API token")
    if dirty:
        user.save()


def find_or_create_user(User, username: str, email: str, password: str):
    user = User.objects.filter(username=username).first()
    if user is None and email:
        user = User.objects.filter(email=email).first()
        if user is not None:
            log(f"SUPERUSER_NAME {username} missing; using email match {getattr(user, 'username', '?')}")
    if user is None:
        qs = User.objects.filter(is_superuser=True)
        if hasattr(qs, "order_by"):
            qs = qs.order_by("pk")
        user = qs.first()
        if user is not None:
            log(f"SUPERUSER_NAME {username} missing; using existing superuser {getattr(user, 'username', '?')}")
    if user is None:
        if not password:
            raise UpsertError("no NetBox superuser and SUPERUSER_PASSWORD empty")
        user = User.objects.create_superuser(username, email, password)
        log(f"created NetBox superuser {username}")
        return user
    promote_user(user)
    return user


def _filter_first(manager, **kwargs):
    qs = manager.filter(**kwargs)
    if hasattr(qs, "select_related"):
        try:
            qs = qs.select_related("user")
        except Exception:
            pass
    return qs.first()


def find_v1_token(Token, token_key: str, v1: int):
    fields = token_field_names(Token)
    manager = Token.objects
    if "plaintext" in fields:
        row = _filter_first(manager, plaintext=token_key)
        if row is not None:
            return row
        if "version" in fields:
            row = _filter_first(manager, version=v1, plaintext=token_key)
            if row is not None:
                return row
    if "key" in fields:
        row = _filter_first(manager, key=token_key)
        if row is not None:
            return row
    return None


def delete_stale_secret_in_key(Token, token_key: str) -> None:
    """Previous upsert wrote the 40-char secret into v2 `key` (max 12)."""
    fields = token_field_names(Token)
    if "key" not in fields:
        return
    try:
        stale = Token.objects.filter(key=token_key)
        n = stale.count() if hasattr(stale, "count") else 0
        if n:
            stale.delete()
            log(f"removed {n} invalid token(s) that stored the secret in key")
    except Exception as exc:
        log_exc("stale key cleanup skipped", exc, token_key)


def _base_kwargs(Token, user, token_key: str, v1: int, use_token_kwarg: bool, use_plaintext: bool, legacy_key: bool) -> dict:
    fields = token_field_names(Token)
    kwargs: dict = {
        "user": user,
        "write_enabled": False,  # write_enabled=False — Core is GET-only
        "enabled": True,
        "description": CORE_DESCRIPTION,
    }
    if "version" in fields and not legacy_key:
        kwargs["version"] = v1
    if "key" in fields and not legacy_key:
        kwargs["key"] = None
    if "pepper_id" in fields:
        kwargs["pepper_id"] = None
    if "hmac_digest" in fields:
        kwargs["hmac_digest"] = None
    if use_token_kwarg:
        kwargs["token"] = token_key
    elif use_plaintext and "plaintext" in fields:
        kwargs["plaintext"] = token_key
    elif legacy_key and "key" in fields:
        kwargs["key"] = token_key
        kwargs.pop("pepper_id", None)
        kwargs.pop("hmac_digest", None)
        kwargs.pop("version", None)
    return kwargs


def _save_instance(row):
    row.save()
    return row


def create_v1_token(Token, user, token_key: str, v1: int):
    """Try official Token(token=) first, then plaintext, objects.create, legacy key, SQL."""
    attempts: list[tuple[str, object]] = [
        ("constructor_token_kwarg", lambda: _save_instance(Token(**_base_kwargs(Token, user, token_key, v1, True, False, False)))),
        ("constructor_plaintext", lambda: _save_instance(Token(**_base_kwargs(Token, user, token_key, v1, False, True, False)))),
        ("objects_create_token", lambda: Token.objects.create(**_base_kwargs(Token, user, token_key, v1, True, False, False))),
        ("objects_create_plaintext", lambda: Token.objects.create(**_base_kwargs(Token, user, token_key, v1, False, True, False))),
        ("legacy_key_field", lambda: _save_instance(Token(**_base_kwargs(Token, user, token_key, v1, False, False, True)))),
        ("raw_sql_v1", lambda: _raw_sql_v1(Token, user, token_key, v1)),
    ]
    errors: list[str] = []
    for name, fn in attempts:
        try:
            row = fn()
            log(f"created read-only v1 API token for Core sync ({name})")
            return row
        except Exception as exc:
            log_exc(f"v1 create {name} failed", exc, token_key)
            errors.append(f"{name}:{type(exc).__name__}")
            existing = None
            try:
                existing = find_v1_token(Token, token_key, v1)
            except Exception:
                existing = None
            if existing is not None:
                log(f"v1 token already in DB after {name} error")
                return existing
    raise UpsertError("all v1 token create methods failed (" + ", ".join(errors) + ")")


def _raw_sql_v1(Token, user, token_key: str, v1: int):
    from django.db import connection

    table = Token._meta.db_table
    columns = {f.name: f.column for f in Token._meta.fields if getattr(f, "column", None)}
    if "plaintext" not in columns or "user" not in columns:
        raise UpsertError("raw SQL skipped: Token has no plaintext/user columns")
    user_id = getattr(user, "pk", None) or getattr(user, "id", None)
    if user_id is None:
        raise UpsertError("raw SQL skipped: user has no pk")
    col_user = columns["user"]
    col_plain = columns["plaintext"]
    sets = {
        col_user: user_id,
        col_plain: token_key,
    }
    if "version" in columns:
        sets[columns["version"]] = v1
    if "write_enabled" in columns:
        sets[columns["write_enabled"]] = False
    if "enabled" in columns:
        sets[columns["enabled"]] = True
    if "description" in columns:
        sets[columns["description"]] = CORE_DESCRIPTION
    if "key" in columns:
        sets[columns["key"]] = None
    if "pepper_id" in columns:
        sets[columns["pepper_id"]] = None
    if "hmac_digest" in columns:
        sets[columns["hmac_digest"]] = None
    if "created" in columns:
        try:
            from django.utils import timezone

            sets[columns["created"]] = timezone.now()
        except Exception:
            pass
    col_list = ", ".join(sets.keys())
    placeholders = ", ".join(["%s"] * len(sets))
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    with connection.cursor() as cursor:
        cursor.execute(sql, list(sets.values()))
    row = find_v1_token(Token, token_key, v1)
    if row is None:
        raise UpsertError("raw SQL insert did not persist")
    return row


def align_existing_token(row, user, token_key: str, v1: int):
    """Read-only, enabled, on the superuser. Convert stray v2 rows that hold our secret."""
    changed = False
    fields = set(getattr(row, "__dict__", {}) or ())
    try:
        fields |= token_field_names(type(row))
    except Exception:
        pass
    if getattr(row, "user_id", None) != getattr(user, "id", getattr(user, "pk", None)):
        try:
            row.user = user
            changed = True
        except Exception as exc:
            log_exc("token user reassign skipped", exc, token_key)
    if getattr(row, "write_enabled", True):
        row.write_enabled = False
        changed = True
    if not getattr(row, "enabled", True):
        row.enabled = True
        changed = True
    if "version" in fields or hasattr(row, "version"):
        if getattr(row, "version", v1) != v1:
            row.version = v1
            if hasattr(row, "key"):
                row.key = None
            if hasattr(row, "pepper_id"):
                row.pepper_id = None
            if hasattr(row, "hmac_digest"):
                row.hmac_digest = None
            if hasattr(row, "plaintext") and not getattr(row, "plaintext", None):
                row.plaintext = token_key
            changed = True
    if changed:
        try:
            row.save()
            log("updated v1 API token to read-only Core sync")
        except Exception as exc:
            log_exc("token update failed; deleting and recreating", exc, token_key)
            try:
                row.delete()
            except Exception as delete_exc:
                log_exc("token delete after update failure", delete_exc, token_key)
                raise
            return None
    else:
        log("API token already present (read-only v1)")
    return row


def attach_dcim_view(user, secret: str) -> None:
    try:
        from django.contrib.auth.models import Permission

        dj_perm = Permission.objects.filter(content_type__app_label="dcim", codename="view_device").first()
        if dj_perm is not None and hasattr(user, "user_permissions"):
            user.user_permissions.add(dj_perm)
    except Exception as exc:
        log_exc("dcim.view_device django permission skipped", exc, secret)
    try:
        from core.models import ObjectType
        from users.models import ObjectPermission

        ot = ObjectType.objects.filter(app_label="dcim", model="device").first()
        if ot is None:
            return
        perm = ObjectPermission.objects.filter(name="ForgeSRE Core read devices").first()
        if perm is None:
            perm = ObjectPermission(name="ForgeSRE Core read devices", enabled=True, actions=["view"])
            perm.save()
        elif list(perm.actions) != ["view"] or not perm.enabled:
            perm.actions = ["view"]
            perm.enabled = True
            perm.save()
        perm.users.add(user)
        perm.object_types.add(ot)
    except Exception as exc:
        log_exc("dcim.view_device object permission skipped", exc, secret)


def load_netbox_token_model():
    """Official NetBox 4.6 imports, with tokens.py fallback if users.models.Token moved."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    Token = None
    import_errors: list[str] = []
    for path in ("users.models", "users.models.tokens"):
        try:
            module = __import__(path, fromlist=["Token"])
            Token = getattr(module, "Token")
            break
        except Exception as exc:
            import_errors.append(f"{path}:{type(exc).__name__}:{redact(exc)}")
    if Token is None:
        raise UpsertError("Token model import failed (" + "; ".join(import_errors) + ")")
    v1 = 1
    try:
        from users.choices import TokenVersionChoices

        v1 = int(TokenVersionChoices.V1)
    except Exception as exc:
        log_exc("TokenVersionChoices missing; using version=1", exc)
    return Token, User, v1


def ensure_django() -> None:
    try:
        from django.apps import apps

        if apps.ready:
            return
    except Exception:
        pass
    netbox_dir = os.environ.get("NETBOX_PATH", "/opt/netbox/netbox")
    if netbox_dir not in sys.path:
        sys.path.insert(0, netbox_dir)
    try:
        os.chdir(netbox_dir)
    except OSError:
        pass
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.environ.get("DJANGO_SETTINGS_MODULE") or "netbox.settings")
    import django

    django.setup()


def upsert_core_token(
    *,
    token_key: str,
    username: str,
    email: str,
    password: str,
    Token,
    User,
    v1: int = 1,
) -> str:
    """Attach Core's token. Skip v1 when secrets already hold a v2 secret."""
    if not token_key:
        log("NETBOX_API_TOKEN empty — Core sync will get HTTP 403")
        return "empty"
    if looks_like_v2_token(token_key):
        log("NETBOX_API_TOKEN is a v2 token — skipping v1 upsert; Core uses Authorization: Bearer")
        return "v2"
    if looks_like_v2_public_key(token_key):
        log("NETBOX_API_TOKEN looks like a 12-character v2 key, not the full nbt_… secret shown once at create")
        raise UpsertError("NETBOX_API_TOKEN looks like a 12-character v2 key, not the full token")
    if len(token_key) != V1_LENGTH:
        log(f"NETBOX_API_TOKEN length {len(token_key)} (v1 plaintext must be 40 characters; openssl rand -hex 20)")
        raise UpsertError("NETBOX_API_TOKEN length is not 40")

    user = find_or_create_user(User, username, email, password)
    delete_stale_secret_in_key(Token, token_key)
    row = find_v1_token(Token, token_key, v1)
    if row is None:
        row = create_v1_token(Token, user, token_key, v1)
    else:
        row = align_existing_token(row, user, token_key, v1)
        if row is None:
            row = create_v1_token(Token, user, token_key, v1)

    owner = find_v1_token(Token, token_key, v1)
    if owner is None:
        raise UpsertError("v1 token upsert did not persist")
    owner_user = getattr(owner, "user", None) or user
    owner_id = getattr(owner, "user_id", None)
    user_id = getattr(user, "id", getattr(user, "pk", None))
    if owner_id is not None and user_id is not None and owner_id != user_id:
        try:
            owner.user = user
            owner.save()
            log(f"reattached v1 token to superuser {user.username}")
            owner_user = user
        except Exception as exc:
            log_exc("reattach to superuser failed", exc, token_key)
            owner_user = getattr(owner, "user", user)
    if not getattr(owner_user, "is_superuser", False):
        raise UpsertError("Core token user is not a NetBox superuser")
    attach_dcim_view(user, token_key)
    uname = getattr(owner_user, "username", getattr(user, "username", "?"))
    log(f"v1 token ready for GET /api/dcim/devices/ (superuser {uname})")
    return "ready"


def main(argv: list[str] | None = None) -> int:
    secret = (os.environ.get("FORGESRE_NB_TOKEN") or "").strip()
    username = (os.environ.get("FORGESRE_NB_USER") or "admin").strip()
    email = (os.environ.get("FORGESRE_NB_EMAIL") or "admin@forgesre.local").strip()
    password = os.environ.get("FORGESRE_NB_PASSWORD") or ""
    try:
        ensure_django()
        Token, User, v1 = load_netbox_token_model()
        upsert_core_token(
            token_key=secret,
            username=username,
            email=email,
            password=password,
            Token=Token,
            User=User,
            v1=v1,
        )
        return 0
    except UpsertError as exc:
        log_exc("could not upsert NetBox API token", exc, secret)
        return 1
    except Exception as exc:
        log_exc("could not upsert NetBox API token", exc, secret)
        # Keep a one-line type hint; full traceback on stderr without the secret.
        tb = redact("".join(traceback.format_exception_only(type(exc), exc)), secret).strip()
        if tb:
            print(tb, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
