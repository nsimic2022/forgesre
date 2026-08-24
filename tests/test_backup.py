from pathlib import Path

from fastapi.testclient import TestClient

from app.backup import (
    BackupLayout,
    CONFIRM_WORD,
    create_backup,
    inspect_archive,
    restore_archive,
    resolve_archive,
)
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.seed import seed
from app.security import hash_password


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _layout(tmp: Path) -> BackupLayout:
    root = Path(__file__).resolve().parents[1]
    return BackupLayout(
        root=root,
        backup_dir=tmp / "backups",
        config_yml=root / "tests" / "forgesre.test.yml",
        dotenv=tmp / ".env",
        secrets=tmp / "secrets.env",
        monitoring_dir=tmp / "monitoring",
        generated_dir=tmp / "generated",
        models_dir=tmp / "models",
        logs_dir=tmp / "logs",
        examples_dir=root / "config" / "examples",
        install_report=tmp / "installation-report.md",
        files_writable=True,
    )


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_backup_archive_includes_db_and_skips_gguf(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    (lay.dotenv).write_text("FORGESRE_HTTP_PORT=8080\n", encoding="utf-8")
    (lay.secrets).write_text("SECRET_KEY=test\n", encoding="utf-8")
    lay.logs_dir.mkdir()
    (lay.logs_dir / "forgesre.log").write_text("hello log\n", encoding="utf-8")
    lay.models_dir.mkdir()
    (lay.models_dir / "model.gguf").write_bytes(b"fake-gguf")
    (lay.models_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    lay.monitoring_dir.mkdir()
    (lay.monitoring_dir / "alerts.local.yml").write_text("groups: []\n", encoding="utf-8")
    result = create_backup(layout=lay)
    assert result.path.is_file()
    assert result.path.name.startswith("forgesre-")
    assert result.path.suffixes[-2:] == [".tar", ".gz"]
    mode = result.path.stat().st_mode & 0o777
    assert mode == 0o600 or mode == 0o400  # umask may clear write bits
    plan = inspect_archive(result.path, lay)
    assert plan.has_db
    assert plan.has_secrets
    blob = str(plan.included) + str(plan.excluded) + str(plan.notes)
    assert "logical database dump" in blob
    assert "model.gguf" in blob or "GGUF" in blob
    import tarfile

    with tarfile.open(result.path, "r:gz") as tar:
        names = tar.getnames()
    joined = "\n".join(names)
    assert "db.json" in joined
    assert "dotenv" in joined
    assert "secrets.env" in joined
    assert "tokenizer.json" in joined
    assert "model.gguf" not in joined
    assert "forgesre.log" in joined
    assert "alerts.local.yml" in joined
    db.close()


def test_backup_no_secrets_omits_secrets_env(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    lay.secrets.write_text("SECRET_KEY=hidden\n", encoding="utf-8")
    result = create_backup(include_secrets=False, layout=lay)
    import tarfile

    with tarfile.open(result.path, "r:gz") as tar:
        names = "\n".join(tar.getnames())
    assert "secrets.env" not in names
    plan = inspect_archive(result.path, lay)
    assert not plan.has_secrets
    db.close()


def test_restore_refuses_without_confirm(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    result = create_backup(layout=lay)
    try:
        restore_archive(result.path, layout=lay)
        raise AssertionError("restore should refuse")
    except PermissionError:
        pass
    db.close()


def test_restore_with_yes_round_trips_user_and_files(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    lay.dotenv.write_text("FORGESRE_HTTP_PORT=8080\n", encoding="utf-8")
    extra = User(
        email="keepme@dc.local",
        name="Keep Me",
        password_hash=hash_password("x"),
        role="viewer",
    )
    db.add(extra)
    db.commit()
    result = create_backup(layout=lay)
    gone = User(
        email="transient@dc.local",
        name="Transient",
        password_hash=hash_password("x"),
        role="viewer",
    )
    db.add(gone)
    db.commit()
    assert db.query(User).filter_by(email="transient@dc.local").one()
    restore_archive(result.path, yes=True, stop_core=False, layout=lay)
    db.expire_all()
    assert db.query(User).filter_by(email="keepme@dc.local").one()
    assert db.query(User).filter_by(email="transient@dc.local").first() is None
    assert lay.dotenv.read_text(encoding="utf-8") == "FORGESRE_HTTP_PORT=8080\n"
    db.close()


def test_admin_backup_buttons_before_appliance_shell(tmp_path, monkeypatch):
    db = _db()
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FORGESRE_RESTORE_STOP_CORE", "0")
    client = TestClient(app)
    _login(client)
    page = client.get("/admin")
    assert page.status_code == 200
    text = page.text
    assert "Platform backup" in text
    assert ">Backup<" in text
    assert ">Import<" in text
    assert "Appliance shell" in text
    assert text.index("Platform backup") < text.index("Appliance shell")
    assert text.index(">Backup<") < text.index("Appliance shell")
    assert "web PTY" in text.lower() or "no terminal in this browser" in text.lower()
    db.close()


def test_admin_create_and_download_backup_is_admin_only(tmp_path, monkeypatch):
    db = _db()
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FORGESRE_RESTORE_STOP_CORE", "0")
    client = TestClient(app)
    anon = client.get("/admin/backups/forgesre-20200101T000000Z.tar.gz", follow_redirects=False)
    assert anon.status_code in {302, 401, 404}
    _login(client)
    created = client.post("/admin/backups", data={}, follow_redirects=False)
    assert created.status_code == 303
    assert "backup=" in created.headers["location"]
    name = created.headers["location"].split("backup=", 1)[1]
    down = client.get(f"/admin/backups/{name}")
    assert down.status_code == 200
    assert down.headers.get("content-type", "").startswith("application/gzip") or "gzip" in down.headers.get("content-type", "")
    assert down.headers.get("cache-control") == "no-store"
    assert down.content[:2] == b"\x1f\x8b" or len(down.content) > 20
    traversal = client.get("/admin/backups/../secrets.env", follow_redirects=False)
    assert traversal.status_code == 404
    db.add(
        User(
            email="ana@dc.local",
            name="Ana",
            password_hash=hash_password("ana-pass"),
            role="analyst",
        )
    )
    db.commit()
    other = TestClient(app)
    other.post("/login", data={"email": "ana@dc.local", "password": "ana-pass"}, follow_redirects=False)
    denied = other.post("/admin/backups", data={}, follow_redirects=False)
    assert denied.status_code == 403
    denied_get = other.get(f"/admin/backups/{name}", follow_redirects=False)
    assert denied_get.status_code == 403
    db.close()


def test_admin_restore_requires_typed_confirm(tmp_path, monkeypatch):
    db = _db()
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FORGESRE_RESTORE_STOP_CORE", "0")
    monkeypatch.setenv("FORGESRE_FILES_WRITABLE", "0")
    client = TestClient(app)
    _login(client)
    created = client.post("/admin/backups", data={}, follow_redirects=False)
    name = created.headers["location"].split("backup=", 1)[1]
    refused = client.post(
        "/admin/backups/restore",
        data={"name": name, "confirm": "please", "acknowledged": "1"},
        follow_redirects=False,
    )
    assert refused.status_code == 400
    ok = client.post(
        "/admin/backups/restore",
        data={"name": name, "confirm": CONFIRM_WORD, "acknowledged": "1"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert "restored=" in ok.headers["location"]
    db.close()


def test_resolve_archive_rejects_bad_names(tmp_path):
    lay = _layout(tmp_path)
    lay.backup_dir.mkdir()
    (lay.backup_dir / "not-a-backup.tgz").write_bytes(b"x")
    try:
        resolve_archive("not-a-backup.tgz", lay)
        raise AssertionError("should reject")
    except ValueError:
        pass
    try:
        resolve_archive("../secrets.env", lay)
        raise AssertionError("should reject")
    except ValueError:
        pass


def test_cli_help_restore_requires_yes():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    restore = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "restore"], text=True)
    assert "--yes" in restore
    assert "docker compose stop core" in restore
    backup = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "backup"], text=True)
    assert "--include-models" in backup
    assert "data/backups" in backup
    overview = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help"], text=True)
    assert "restore" in overview
    assert "--include-models" in overview
