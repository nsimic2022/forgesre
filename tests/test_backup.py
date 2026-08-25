from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from app.backup import (
    ARCHIVE_RE,
    BackupLayout,
    CONFIRM_WORD,
    FOLDER_RE,
    INNER_ARCHIVE,
    archive_file,
    create_backup,
    delete_backup,
    inspect_archive,
    list_archives,
    restore_archive,
    resolve_archive,
    resolve_cli_archive,
    save_upload,
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
    assert result.path.name == INNER_ARCHIVE
    assert FOLDER_RE.match(result.path.parent.name)
    assert result.name == result.path.parent.name
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
    created = client.post("/admin/backups", data={}, follow_redirects=False)
    assert created.status_code == 303
    name = created.headers["location"].split("backup=", 1)[1]
    page = client.get("/admin")
    text = page.text
    assert 'select name="name"' in text
    assert "Backup folder (newest first)" in text
    assert name in text
    assert "UTC" in text
    assert "SECRET_KEY" not in text
    assert ">Download</a>" in text
    assert ">Remove<" in text
    assert text.index(">Download</a>") < text.index(">Remove<")
    assert 'class="backup-list"' in text
    assert "/admin/backups/remove" in text
    assert "onsubmit=" in text
    assert "confirm(" in text
    restore = text.split('action="/admin/backups/restore"', 1)[1]
    assert restore.find('type="checkbox"') < restore.find("I understand this overwrites")
    assert restore.find('name="acknowledged"') < restore.find("<span>I understand this overwrites")
    css = Path(__file__).resolve().parents[1].joinpath("frontend", "static", "app.css").read_text(encoding="utf-8")
    assert ".backup-list td.row-actions { gap: 1rem;" in css
    assert "form label.inline-check" in css
    assert "flex-direction: row" in css.split("form label.inline-check", 1)[1].split("label.inline-check input", 1)[0]
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
    assert "backup_" in restore
    assert "Pick a number" in restore or "restore 1" in restore
    assert "./forgesre import" in restore
    assert "import backup" in restore
    backup = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "backup"], text=True)
    assert "--include-models" in backup
    assert "data/backups" in backup
    assert "backup_" in backup
    assert "forgesre.tar.gz" in backup
    overview = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help"], text=True)
    assert "restore" in overview
    assert "remove" in overview
    assert "--include-models" in overview
    assert "docker compose exec postgres" in backup
    update = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "update"], text=True)
    assert "snmp-exporter" in update
    assert "sqlalchemy" in update.lower()
    remove = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "remove"], text=True)
    assert "remove backup" in remove
    assert "--yes" in remove
    assert "data/backups" in remove or "backup_" in remove


def test_backup_py_has_no_module_level_sqlalchemy_import():
    import ast

    source = (Path(__file__).resolve().parents[1] / "backend" / "app" / "backup.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sqlalchemy"), alias.name
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("sqlalchemy")


def test_backup_help_runs_without_sqlalchemy():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    blocker = r"""
import builtins
import sys

real = builtins.__import__


def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "sqlalchemy" or name.startswith("sqlalchemy."):
        raise ModuleNotFoundError(name)
    return real(name, globals, locals, fromlist, level)


builtins.__import__ = blocked
sys.path.insert(0, "backend")
from app import backup

assert backup.FORMAT_VERSION == 1
raise SystemExit(backup.main(["help"]))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "backend")
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backup" in result.stdout.lower()
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_postgres_dump_and_restore_use_compose_psql(monkeypatch):
    from app.backup import dump_database, restore_database

    sqls: list[str] = []

    def fake_psql(sql: str, *, root=None) -> str:
        sqls.append(sql)
        if "pg_tables" in sql:
            return "users\nplaybooks\n"
        if "jsonb_agg" in sql and "users" in sql:
            return '[{"id":1,"email":"ops@dc.local"}]'
        if "jsonb_agg" in sql:
            return "[]"
        return ""

    monkeypatch.setenv("FORGESRE_BACKUP_PG", "1")
    monkeypatch.setattr("app.backup._psql", fake_psql)
    payload = dump_database()
    assert payload["users"] == [{"id": 1, "email": "ops@dc.local"}]
    assert payload["playbooks"] == []
    restore_database({"users": [{"id": 1, "email": "ops@dc.local"}], "playbooks": []})
    joined = "\n".join(sqls)
    assert "json_populate_recordset" in joined
    assert "TRUNCATE" in joined
    assert "docker compose" not in joined  # mocked; no live docker


def test_backup_cli_dump_failure_is_clear_error(monkeypatch, capsys):
    from app import backup as backup_mod

    def fake_create(**_kwargs):
        return backup_mod.BackupResult(
            path=Path("/tmp/forgesre-x.tar.gz"),
            name="forgesre-x.tar.gz",
            notes=["database dump failed: connection refused"],
            db=False,
        )

    monkeypatch.setattr(backup_mod, "create_backup", fake_create)
    rc = backup_mod.main(["backup"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR: database dump failed" in captured.err
    assert "Traceback" not in captured.err
    assert "ModuleNotFoundError" not in captured.err


def test_backup_cli_missing_sqlalchemy_has_no_traceback(monkeypatch, capsys):
    from app import backup as backup_mod

    def boom(**_kwargs):
        raise ModuleNotFoundError("sqlalchemy")

    monkeypatch.setattr(backup_mod, "create_backup", boom)
    rc = backup_mod.main(["backup"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Backup failed:" in captured.err
    assert "Traceback" not in captured.err


def test_update_script_starts_snmp_and_survives_backup_failure():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "snmp-exporter" in text
    assert "Backup failed" in text
    assert "Continuing with render-monitoring" in text


def test_compose_snmp_exporter_is_default_service():
    import yaml

    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = data["services"]["snmp-exporter"]
    assert "profiles" not in svc
    assert svc["network_mode"] == "host"
    assert any("9116" in str(part) for part in svc["command"])


def test_backup_writes_run_folder_not_loose_files(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    lay.dotenv.write_text("FORGESRE_HTTP_PORT=8080\n", encoding="utf-8")
    result = create_backup(layout=lay)
    assert result.path.name == INNER_ARCHIVE
    assert FOLDER_RE.match(result.path.parent.name)
    manifest = result.path.parent / "MANIFEST.txt"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert INNER_ARCHIVE in text
    assert "logical database dump" in text or "Included:" in text
    loose = [p.name for p in lay.backup_dir.iterdir() if p.is_file()]
    assert loose == []
    dirs = [p for p in lay.backup_dir.iterdir() if p.is_dir()]
    assert len(dirs) == 1
    listed = list_archives(lay)
    assert listed[0]["name"] == result.name
    assert resolve_archive(result.name, lay) == result.path
    assert archive_file(result.path.parent) == result.path
    assert archive_file(result.path) == result.path
    restore_archive(result.path.parent, yes=True, stop_core=False, layout=lay)
    db.close()


def test_legacy_root_tar_still_lists_and_resolves(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    result = create_backup(layout=lay)
    stamp = result.path.parent.name[len("backup_") :]
    assert ARCHIVE_RE.match(f"forgesre-{stamp}.tar.gz")
    legacy = lay.backup_dir / f"forgesre-{stamp}.tar.gz"
    shutil.move(str(result.path), str(legacy))
    names = [row["name"] for row in list_archives(lay)]
    assert legacy.name in names
    assert resolve_archive(legacy.name, lay) == legacy.resolve()
    db.close()


def test_save_upload_stores_tar_in_run_folder(tmp_path):
    db = _db()
    lay = _layout(tmp_path)
    result = create_backup(layout=lay)
    blob = result.path.read_bytes()
    stored = save_upload(blob, "forgesre-20200101T000000Z.tar.gz", layout=lay)
    assert stored.name == INNER_ARCHIVE
    assert stored.parent.name == "backup_20200101T000000Z"
    assert (stored.parent / "MANIFEST.txt").is_file()
    assert resolve_archive("backup_20200101T000000Z", lay) == stored
    db.close()


def _touch_run(lay: BackupLayout, stamp: str, payload: bytes = b"x" * 40) -> None:
    folder = lay.backup_dir / f"backup_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / INNER_ARCHIVE).write_bytes(payload)


def test_list_archives_newest_first_with_timestamp_labels(tmp_path):
    lay = _layout(tmp_path)
    _touch_run(lay, "20200101T000000Z")
    _touch_run(lay, "20260824T180000Z")
    listed = list_archives(lay)
    assert [row["name"] for row in listed] == [
        "backup_20260824T180000Z",
        "backup_20200101T000000Z",
    ]
    assert listed[0]["label"].startswith("2026-08-24 18:00:00 UTC")
    assert "backup_20260824T180000Z" in listed[0]["label"]
    assert "SECRET" not in listed[0]["label"]
    assert "secrets.env" not in listed[0]["label"]
    assert resolve_cli_archive("1", rows=listed, layout=lay) == lay.backup_dir / "backup_20260824T180000Z" / INNER_ARCHIVE
    try:
        resolve_cli_archive("9", rows=listed, layout=lay)
        raise AssertionError("out of range should fail")
    except ValueError:
        pass


def test_cli_restore_without_path_lists_numbered_picker(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["restore"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "1." in captured.out
    assert "backup_20260824T120000Z" in captured.out
    assert "2026-08-24 12:00:00 UTC" in captured.out
    assert "Pick a number" in captured.out
    assert "SECRET_KEY" not in captured.out
    assert "Traceback" not in captured.err


def test_cli_import_alias_lists_same_picker(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["import"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "1." in captured.out
    assert "backup_20260824T120000Z" in captured.out


def test_cli_restore_number_prints_plan_without_yes(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    db = _db()
    lay = _layout(tmp_path)
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    monkeypatch.setenv("FORGESRE_RESTORE_STOP_CORE", "0")
    create_backup(layout=lay)
    rc = backup_mod.main(["restore", "1"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Would overwrite" in captured.out
    assert "--yes" in captured.out
    db.close()


def test_cli_restore_path_still_prints_plan(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    db = _db()
    lay = _layout(tmp_path)
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    result = create_backup(layout=lay)
    rc = backup_mod.main(["restore", str(result.path.parent)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Would overwrite" in captured.out
    db.close()


def test_restore_picker_runs_without_sqlalchemy(tmp_path):
    import os
    import subprocess
    import sys

    folder = tmp_path / "backups" / "backup_20260824T120000Z"
    folder.mkdir(parents=True)
    (folder / INNER_ARCHIVE).write_bytes(b"x" * 40)
    blocker = r"""
import builtins
import sys

real = builtins.__import__


def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "sqlalchemy" or name.startswith("sqlalchemy."):
        raise ModuleNotFoundError(name)
    return real(name, globals, locals, fromlist, level)


builtins.__import__ = blocked
sys.path.insert(0, "backend")
from app import backup

raise SystemExit(backup.main(["restore"]))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "backend")
    env["FORGESRE_BACKUP_DIR"] = str(tmp_path / "backups")
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1." in result.stdout
    assert "backup_20260824T120000Z" in result.stdout
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "sqlalchemy" not in result.stderr


def test_delete_backup_removes_run_folder_only(tmp_path):
    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    _touch_run(lay, "20260824T130000Z")
    keep = lay.backup_dir / "backup_20260824T130000Z"
    gone = delete_backup("backup_20260824T120000Z", lay)
    assert gone.name == "backup_20260824T120000Z"
    assert not (lay.backup_dir / "backup_20260824T120000Z").exists()
    assert keep.is_dir()
    assert (keep / INNER_ARCHIVE).is_file()
    assert lay.backup_dir.is_dir()
    names = [row["name"] for row in list_archives(lay)]
    assert names == ["backup_20260824T130000Z"]


def test_delete_backup_legacy_tar_unlinks_file_only(tmp_path):
    lay = _layout(tmp_path)
    lay.backup_dir.mkdir()
    legacy = lay.backup_dir / "forgesre-20260824T120000Z.tar.gz"
    leftover = lay.backup_dir / "backup_20260824T130000Z"
    leftover.mkdir()
    (leftover / INNER_ARCHIVE).write_bytes(b"x" * 40)
    legacy.write_bytes(b"y" * 40)
    deleted = delete_backup(legacy.name, lay)
    assert deleted.name == legacy.name
    assert not legacy.exists()
    assert leftover.is_dir()
    assert lay.backup_dir.is_dir()


def test_delete_backup_cannot_escape_dir(tmp_path):
    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    secret = tmp_path / "secrets.env"
    secret.write_text("SECRET_KEY=hidden\n", encoding="utf-8")
    outside = tmp_path / "not-backups"
    outside.mkdir()
    (outside / "keep.txt").write_text("stay\n", encoding="utf-8")
    try:
        delete_backup("../secrets.env", lay)
        raise AssertionError("should reject")
    except ValueError:
        pass
    try:
        delete_backup("..", lay)
        raise AssertionError("should reject")
    except ValueError:
        pass
    try:
        delete_backup(".", lay)
        raise AssertionError("should reject")
    except ValueError:
        pass
    try:
        delete_backup(str(lay.backup_dir), lay)
        raise AssertionError("should reject backups root")
    except (ValueError, FileNotFoundError):
        pass
    assert secret.read_text(encoding="utf-8") == "SECRET_KEY=hidden\n"
    assert (outside / "keep.txt").is_file()
    assert (lay.backup_dir / "backup_20260824T120000Z").is_dir()
    assert lay.backup_dir.is_dir()


def test_admin_remove_backup_is_admin_only(tmp_path, monkeypatch):
    db = _db()
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FORGESRE_RESTORE_STOP_CORE", "0")
    client = TestClient(app)
    anon = client.post("/admin/backups/remove", data={"name": "backup_x"}, follow_redirects=False)
    assert anon.status_code in {302, 401, 403, 404, 422}
    _login(client)
    first = client.post("/admin/backups", data={}, follow_redirects=False)
    name = first.headers["location"].split("backup=", 1)[1]
    other_dir = tmp_path / "backups" / "backup_20200101T000000Z"
    other_dir.mkdir()
    (other_dir / INNER_ARCHIVE).write_bytes(b"x" * 40)
    page = client.get("/admin")
    text = page.text
    assert ">Download</a>" in text
    assert ">Remove<" in text
    assert 'action="/admin/backups/remove"' in text
    assert "onsubmit=" in text
    assert "confirm(" in text
    assert text.index(">Download</a>") < text.index(">Remove<")
    traversal = client.post(
        "/admin/backups/remove",
        data={"name": "../secrets.env"},
        follow_redirects=False,
    )
    assert traversal.status_code == 404
    db.add(
        User(
            email="ana-remove@dc.local",
            name="Ana Remove",
            password_hash=hash_password("ana-pass"),
            role="analyst",
        )
    )
    db.commit()
    analyst = TestClient(app)
    analyst.post("/login", data={"email": "ana-remove@dc.local", "password": "ana-pass"}, follow_redirects=False)
    denied = analyst.post("/admin/backups/remove", data={"name": name}, follow_redirects=False)
    assert denied.status_code == 403
    removed = client.post("/admin/backups/remove", data={"name": name}, follow_redirects=False)
    assert removed.status_code == 303
    assert f"removed={name}" in removed.headers["location"]
    listed = [row["name"] for row in list_archives()]
    assert name not in listed
    assert "backup_20200101T000000Z" in listed
    db.close()


def test_cli_remove_without_path_lists_numbered_picker(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["remove"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "1." in captured.out
    assert "backup_20260824T120000Z" in captured.out
    assert "remove backup" in captured.out
    assert (lay.backup_dir / "backup_20260824T120000Z").is_dir()
    assert "Traceback" not in captured.err


def test_cli_remove_backup_token_lists_same_picker(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["remove", "backup"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "1." in captured.out
    assert "backup_20260824T120000Z" in captured.out
    assert (lay.backup_dir / "backup_20260824T120000Z").is_dir()


def test_cli_import_backup_token_lists_same_picker(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["import", "backup"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "1." in captured.out
    assert "backup_20260824T120000Z" in captured.out
    assert "Pick a number" in captured.out


def test_cli_remove_number_without_yes_refuses(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["remove", "backup", "1"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Would remove" in captured.out
    assert "--yes" in captured.out
    assert (lay.backup_dir / "backup_20260824T120000Z").is_dir()


def test_cli_remove_yes_deletes_one_folder(tmp_path, monkeypatch, capsys):
    from app import backup as backup_mod

    lay = _layout(tmp_path)
    _touch_run(lay, "20260824T120000Z")
    _touch_run(lay, "20260824T130000Z")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(lay.backup_dir))
    rc = backup_mod.main(["remove", "backup", "2", "--yes"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "Removed backup_20260824T120000Z" in captured.out
    assert not (lay.backup_dir / "backup_20260824T120000Z").exists()
    assert (lay.backup_dir / "backup_20260824T130000Z").is_dir()
    assert lay.backup_dir.is_dir()


def test_remove_picker_runs_without_sqlalchemy(tmp_path):
    import os
    import subprocess
    import sys

    folder = tmp_path / "backups" / "backup_20260824T120000Z"
    folder.mkdir(parents=True)
    (folder / INNER_ARCHIVE).write_bytes(b"x" * 40)
    blocker = r"""
import builtins
import sys

real = builtins.__import__


def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "sqlalchemy" or name.startswith("sqlalchemy."):
        raise ModuleNotFoundError(name)
    return real(name, globals, locals, fromlist, level)


builtins.__import__ = blocked
sys.path.insert(0, "backend")
from app import backup

raise SystemExit(backup.main(["remove", "backup"]))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "backend")
    env["FORGESRE_BACKUP_DIR"] = str(tmp_path / "backups")
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1." in result.stdout
    assert "backup_20260824T120000Z" in result.stdout
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "sqlalchemy" not in result.stderr

