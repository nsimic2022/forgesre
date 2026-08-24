"""Platform backup and restore. Same archive for ./forgesre backup|restore and /admin.

Each run is one folder under data/backups/ (gitignored, dir mode 700):

  data/backups/backup_YYYYMMDDTHHMMSSZ/forgesre.tar.gz
  data/backups/backup_YYYYMMDDTHHMMSSZ/MANIFEST.txt

The restore unit is the tar.gz (db dump, yml, .env, secrets, generated, logs,
examples, installation-report inside). Do not explode it into loose files at
the backups root. Legacy data/backups/forgesre-*.tar.gz files are still read.

Host CLI must not import sqlalchemy at module load (host Python has no Core
venv). GUI/Core dumps via SQLAlchemy; host dumps via docker compose exec
postgres. Both write db.json in the same tar.gz format.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ARCHIVE_RE = re.compile(r"^forgesre-\d{8}T\d{6}Z\.tar\.gz$")
FOLDER_RE = re.compile(r"^backup_\d{8}T\d{6}Z$")
INNER_ARCHIVE = "forgesre.tar.gz"
TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CONFIRM_WORD = "RESTORE"
MODEL_SMALL_MAX = 80 * 1024 * 1024
LOG_FILE_MAX = 500 * 1024 * 1024
FORMAT_VERSION = 1

EXCLUDED_ALWAYS = [
    "Docker / container images (re-pull with ./forgesre update)",
    "Prometheus TSDB (data/prometheus) — 15-day metrics; re-scrapes after restore",
    "Loki chunks (data/loki) and Alloy WAL",
    "Grafana SQLite (data/grafana) — provisioned dashboards come from the repo",
    "Postgres data dir (data/postgres) — logical dump is used instead",
    "Nested archives under data/backups/",
    "Optional mailbox mailboxes (data/dms) unless you copy them separately",
]


@dataclass
class BackupLayout:
    root: Path
    backup_dir: Path
    config_yml: Path
    dotenv: Path
    secrets: Path
    monitoring_dir: Path
    generated_dir: Path
    models_dir: Path
    logs_dir: Path
    examples_dir: Path
    install_report: Path
    files_writable: bool = True


@dataclass
class BackupResult:
    path: Path
    name: str
    included: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    secrets: bool = True
    db: bool = False


@dataclass
class RestorePlan:
    archive: Path
    included: list[str]
    excluded: list[str]
    notes: list[str]
    has_secrets: bool
    has_db: bool
    files_writable: bool


def _repo_root() -> Path:
    host = os.environ.get("FORGESRE_HOST_ROOT", "").strip()
    if host:
        return Path(host)
    return Path(__file__).resolve().parents[2]


def _data_dir(root: Path) -> Path:
    raw = os.environ.get("FORGESRE_DATA", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (root / path)
    env_file = Path(os.environ.get("FORGESRE_DOTENV") or (root / ".env"))
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("FORGESRE_DATA="):
                value = line.split("=", 1)[1].strip().strip('"')
                path = Path(value)
                return path if path.is_absolute() else (root / path)
    return root / "data"


def layout_from_env() -> BackupLayout:
    """Resolve host or Core-container paths. Extra env vars are set in compose.

    Stdlib-only: the host CLI must not import sqlalchemy or PyYAML.
    """
    root = _repo_root()
    data = _data_dir(root)
    backup = Path(os.environ.get("FORGESRE_BACKUP_DIR") or (data / "backups"))
    host_root_set = bool(os.environ.get("FORGESRE_HOST_ROOT", "").strip())
    files_flag = os.environ.get("FORGESRE_FILES_WRITABLE", "").strip().lower()
    if files_flag in {"0", "false", "no"}:
        files_writable = False
    elif files_flag in {"1", "true", "yes"}:
        files_writable = True
    else:
        files_writable = not host_root_set
    return BackupLayout(
        root=root,
        backup_dir=backup,
        config_yml=Path(os.environ.get("FORGESRE_CONFIG") or (root / "config" / "forgesre.yml")),
        dotenv=Path(os.environ.get("FORGESRE_DOTENV") or (root / ".env")),
        secrets=Path(os.environ.get("FORGESRE_SECRETS_FILE") or (root / "secrets" / "secrets.env")),
        monitoring_dir=Path(os.environ.get("FORGESRE_MONITORING_DIR") or (root / "monitoring")),
        generated_dir=Path(os.environ.get("FORGESRE_GENERATED_DIR") or (data / "generated")),
        models_dir=Path(os.environ.get("FORGESRE_MODELS_DIR") or (data / "models")),
        logs_dir=Path(os.environ.get("FORGESRE_LOGS_DIR") or (data / "logs")),
        examples_dir=root / "config" / "examples",
        install_report=root / "installation-report.md",
        files_writable=files_writable,
    )


def stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_backup_dir(layout: BackupLayout | None = None) -> Path:
    lay = layout or layout_from_env()
    lay.backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lay.backup_dir, 0o700)
    except OSError:
        pass
    return lay.backup_dir


def is_run_folder_name(name: str) -> bool:
    return bool(FOLDER_RE.match(Path(name).name))


def is_archive_name(name: str) -> bool:
    raw = Path(name).name
    return raw == INNER_ARCHIVE or bool(ARCHIVE_RE.match(raw))


def backup_ident(path: Path) -> str:
    """GUI/CLI name for a restore unit: run folder, or legacy tar at backups root."""
    path = Path(path)
    if is_run_folder_name(path.name) and path.is_dir():
        return path.name
    if is_run_folder_name(path.parent.name):
        return path.parent.name
    return path.name


def download_name(path: Path) -> str:
    """Content-Disposition name so two downloads do not collide as forgesre.tar.gz."""
    ident = backup_ident(path)
    if is_run_folder_name(ident):
        return f"forgesre-{ident[len('backup_'):]}.tar.gz"
    return Path(path).name


def tar_in_run_dir(folder: Path) -> Path:
    inner = folder / INNER_ARCHIVE
    if inner.is_file():
        return inner
    legacy = sorted(
        p for p in folder.glob("forgesre-*.tar.gz") if p.is_file() and ARCHIVE_RE.match(p.name)
    )
    if legacy:
        return legacy[-1]
    raise FileNotFoundError(f"no {INNER_ARCHIVE} in {folder.name}")


def archive_file(path: Path) -> Path:
    """Accept a run folder or a tar.gz. Returns the tar path."""
    path = Path(path)
    if path.is_dir():
        return tar_in_run_dir(path)
    if path.is_file():
        return path
    raise FileNotFoundError(path.name)


def resolve_archive(name: str, layout: BackupLayout | None = None) -> Path:
    lay = layout or layout_from_env()
    base = ensure_backup_dir(lay).resolve()
    raw = str(name).strip().replace("\\", "/").strip("/")
    if not raw or ".." in Path(raw).parts:
        raise ValueError("not a ForgeSRE backup archive name")
    path = (base / raw).resolve()
    if base not in path.parents and path != base:
        raise ValueError("backup path escapes data/backups")
    if path.is_dir():
        if path == base or not is_run_folder_name(path.name):
            raise ValueError("not a ForgeSRE backup folder name")
        return tar_in_run_dir(path)
    if path.is_file():
        if path.name == INNER_ARCHIVE or ARCHIVE_RE.match(path.name):
            return path
        raise ValueError("not a ForgeSRE backup archive name")
    raise FileNotFoundError(Path(raw).name)


def delete_backup(name: str, layout: BackupLayout | None = None) -> Path:
    """Delete one run folder (tar + MANIFEST) or one legacy root tar.

    Never deletes data/, data/backups/, or anything outside backup_dir.
    """
    lay = layout or layout_from_env()
    base = ensure_backup_dir(lay).resolve()
    tar = resolve_archive(name, lay).resolve()
    if tar == base or base not in tar.parents:
        raise ValueError("refusing to delete the backups directory")
    parent = tar.parent
    if parent == base:
        if not ARCHIVE_RE.match(tar.name) or not tar.is_file():
            raise ValueError("refusing to delete this backup file")
        tar.unlink()
        return tar
    if parent.parent != base or not is_run_folder_name(parent.name) or parent == base:
        raise ValueError("refusing to delete outside a single backup run folder")
    shutil.rmtree(parent)
    return parent


def resolve_cli_archive(
    archive_arg: str,
    *,
    rows: list[dict[str, Any]] | None = None,
    layout: BackupLayout | None = None,
) -> Path:
    """Accept a 1-based picker index, a run folder, or a tar.gz path."""
    raw = str(archive_arg).strip()
    if not raw:
        raise ValueError("no backup selected")
    if raw.isdigit():
        listed = rows if rows is not None else list_archives(layout)
        idx = int(raw)
        if idx < 1 or idx > len(listed):
            raise ValueError(f"no backup numbered {raw}")
        return resolve_archive(listed[idx - 1]["name"], layout)
    path = Path(raw)
    if path.exists():
        return archive_file(path)
    return resolve_archive(raw, layout)


def stamp_from_backup_name(name: str) -> str:
    """UTC stamp embedded in backup_… / forgesre-….tar.gz names. Empty if unknown."""
    raw = Path(name).name
    if is_run_folder_name(raw):
        return raw[len("backup_") :]
    if ARCHIVE_RE.match(raw):
        return raw[len("forgesre-") : -len(".tar.gz")]
    return ""


def pretty_backup_stamp(stamp: str) -> str:
    try:
        dt = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return ""


def backup_label(name: str, size: int) -> str:
    """Dropdown/CLI line: timestamp + folder name + size. Never archive contents."""
    pretty = pretty_backup_stamp(stamp_from_backup_name(name))
    sized = format_size(size)
    if pretty:
        return f"{pretty} — {name} ({sized})"
    return f"{name} ({sized})"


def _archive_row(name: str, size: int, mtime: datetime) -> dict[str, Any]:
    stamp = stamp_from_backup_name(name)
    return {
        "name": name,
        "size": size,
        "mtime": mtime.isoformat(),
        "stamp": stamp,
        "label": backup_label(name, size),
    }


def _row_when(row: dict[str, Any]) -> datetime:
    stamp = str(row.get("stamp") or "")
    if stamp:
        try:
            return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(str(row["mtime"]))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def list_archives(layout: BackupLayout | None = None) -> list[dict[str, Any]]:
    lay = layout or layout_from_env()
    folder = ensure_backup_dir(lay)
    rows: list[dict[str, Any]] = []
    for child in folder.iterdir():
        if child.is_dir() and is_run_folder_name(child.name):
            try:
                tar = tar_in_run_dir(child)
            except FileNotFoundError:
                continue
            stat = tar.stat()
            rows.append(
                _archive_row(
                    child.name,
                    stat.st_size,
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                )
            )
        elif child.is_file() and ARCHIVE_RE.match(child.name):
            stat = child.stat()
            rows.append(
                _archive_row(
                    child.name,
                    stat.st_size,
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                )
            )
    rows.sort(key=_row_when, reverse=True)
    return rows


def print_backup_picker(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No backups under data/backups/. Run ./forgesre backup first.")
        return
    print("Backups (newest first). Restore unit is forgesre.tar.gz inside each folder.")
    for index, row in enumerate(rows, 1):
        print(f"  {index}. {row['label']}")


def read_picker_choice() -> str:
    import sys

    if not sys.stdin.isatty():
        return ""
    try:
        return input("Pick a number: ").strip()
    except EOFError:
        return ""


def read_yes_confirm(prompt: str = "Type yes to delete this backup folder only: ") -> bool:
    import sys

    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() == "yes"
    except EOFError:
        return False


def _pop_backup_noun(args: list[str]) -> list[str]:
    """Allow `import backup` / `remove backup` as two tokens before the picker index."""
    if args and args[0] == "backup":
        return args[1:]
    return args


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _sqlalchemy_ready() -> bool:
    """True inside Core / pytest. False on the host CLI (no sqlalchemy)."""
    if os.environ.get("FORGESRE_BACKUP_PG", "").strip().lower() in {"1", "true", "yes"}:
        return False
    try:
        import sqlalchemy  # noqa: F401
        from app.db import Base, engine  # noqa: F401

        return True
    except ImportError:
        return False


def _compose_argv(root: Path | None = None) -> list[str]:
    cwd = str(root or _repo_root())
    for prefix in (["docker", "compose"], ["sudo", "docker", "compose"]):
        try:
            result = subprocess.run(
                [*prefix, "version"],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return prefix
    raise RuntimeError(
        "docker compose is not available. Start Docker, or run backup from Administration (Core)."
    )


def _psql(sql: str, *, root: Path | None = None) -> str:
    """Run SQL in the bundled Postgres container. Host CLI path — no sqlalchemy."""
    cwd = str(root or _repo_root())
    cmd = [
        *_compose_argv(root),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "forgesre",
        "-d",
        "forgesre",
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        "-q",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            input=sql,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"psql via docker compose failed: {exc}") from exc
    if result.returncode != 0:
        extra = (result.stderr or result.stdout or str(result.returncode)).strip()
        raise RuntimeError(
            "Postgres dump/restore failed via docker compose exec postgres. "
            f"{extra}. Start it: docker compose up -d postgres"
        )
    return result.stdout or ""


def _quote_ident(name: str) -> str:
    if not TABLE_NAME_RE.match(name):
        raise ValueError(f"refusing table name {name!r}")
    return '"' + name.replace('"', "") + '"'


def _dollar_quote(payload: str) -> str:
    tag = "fsre"
    n = 0
    while f"${tag}$" in payload:
        n += 1
        tag = f"fsre{n}"
    return f"${tag}${payload}${tag}$"


def _public_tables() -> list[str]:
    raw = _psql("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename;")
    names = []
    for line in raw.splitlines():
        name = line.strip()
        if name and TABLE_NAME_RE.match(name):
            names.append(name)
    return names


def _dump_database_sqlalchemy() -> dict[str, list[dict[str, Any]]]:
    from app.db import Base, engine

    payload: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(table.select()).mappings().all()
            payload[table.name] = [{k: _jsonable(v) for k, v in dict(row).items()} for row in rows]
    return payload


def _dump_database_postgres() -> dict[str, list[dict[str, Any]]]:
    """Logical dump via psql json — same db.json shape as the SQLAlchemy path."""
    payload: dict[str, list[dict[str, Any]]] = {}
    tables = _public_tables()
    if not tables:
        raise RuntimeError(
            "Postgres has no public tables (container down or empty). "
            "Start it: docker compose up -d postgres"
        )
    for name in tables:
        ident = _quote_ident(name)
        raw = _psql(f"SELECT COALESCE(jsonb_agg(to_jsonb(t)), '[]'::jsonb) FROM {ident} AS t;")
        blob = raw.strip() or "[]"
        try:
            rows = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Postgres JSON dump for {name} was not valid JSON") from exc
        if not isinstance(rows, list):
            raise RuntimeError(f"Postgres JSON dump for {name} was not a list")
        payload[name] = rows
    return payload


def dump_database() -> dict[str, list[dict[str, Any]]]:
    """GUI/Core uses SQLAlchemy. Host CLI uses docker compose exec postgres (no pip on the host)."""
    if _sqlalchemy_ready():
        return _dump_database_sqlalchemy()
    return _dump_database_postgres()


def _coerce_row(table: Any, row: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import DateTime as SADateTime

    out: dict[str, Any] = {}
    for col in table.columns:
        if col.name not in row:
            continue
        val = row[col.name]
        if val is None:
            out[col.name] = None
            continue
        if isinstance(col.type, SADateTime) and isinstance(val, str):
            out[col.name] = datetime.fromisoformat(val)
        else:
            out[col.name] = val
    return out


def _restore_database_sqlalchemy(payload: dict[str, list[dict[str, Any]]]) -> None:
    from sqlalchemy import text
    from app.db import Base, engine

    dialect = engine.dialect.name
    tables = list(Base.metadata.sorted_tables)
    with engine.begin() as conn:
        if dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            for table in reversed(tables):
                conn.execute(table.delete())
        else:
            names = ", ".join(table.name for table in tables)
            conn.exec_driver_sql(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
        for table in tables:
            rows = [_coerce_row(table, row) for row in payload.get(table.name, [])]
            if rows:
                conn.execute(table.insert(), rows)
        if dialect == "postgresql":
            for table in tables:
                if "id" not in table.c:
                    continue
                try:
                    conn.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {table.name}), 1))"
                        )
                    )
                except Exception:  # noqa: BLE001 — table may have no serial
                    pass
        if dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _restore_database_postgres(payload: dict[str, list[dict[str, Any]]]) -> None:
    tables = _public_tables()
    if not tables:
        raise RuntimeError(
            "Postgres has no public tables to restore into. Start it: docker compose up -d postgres"
        )
    quoted = ", ".join(_quote_ident(name) for name in tables)
    parts = [
        "SET session_replication_role = replica;",
        f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE;",
    ]
    for name in tables:
        rows = payload.get(name) or []
        if not rows:
            continue
        ident = _quote_ident(name)
        blob = json.dumps(rows, ensure_ascii=False)
        parts.append(
            f"INSERT INTO {ident} SELECT * FROM json_populate_recordset(NULL::{ident}, {_dollar_quote(blob)});"
        )
        parts.append(
            "DO $seq$\n"
            "BEGIN\n"
            f"  IF pg_get_serial_sequence('{name}', 'id') IS NOT NULL THEN\n"
            f"    PERFORM setval(pg_get_serial_sequence('{name}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {ident}), 1));\n"
            "  END IF;\n"
            "EXCEPTION WHEN undefined_column OR undefined_object THEN\n"
            "  NULL;\n"
            "END $seq$;"
        )
    parts.append("SET session_replication_role = DEFAULT;")
    _psql("\n".join(parts))


def restore_database(payload: dict[str, list[dict[str, Any]]]) -> None:
    if _sqlalchemy_ready():
        _restore_database_sqlalchemy(payload)
        return
    _restore_database_postgres(payload)


def _copy_file(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return True


def _copy_tree(src: Path, dest: Path, skip: Callable[[Path], bool]) -> list[str]:
    copied: list[str] = []
    if not src.is_dir():
        return copied
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if skip(path):
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(str(path.relative_to(src)))
    return copied


def models_dir_is_small(models_dir: Path) -> bool:
    if not models_dir.is_dir():
        return True
    total = 0
    for path in models_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".gguf":
            return False
        total += path.stat().st_size
        if total >= MODEL_SMALL_MAX:
            return False
    return True


def _write_run_manifest(
    run_dir: Path,
    stamp: str,
    included: list[str],
    excluded: list[str],
    notes: list[str],
) -> None:
    lines = [
        f"ForgeSRE backup {stamp}",
        f"Archive: {INNER_ARCHIVE}",
        f"Created: {datetime.now(timezone.utc).isoformat()}",
        "",
        "One restore unit = this folder. Import the .tar.gz (do not unpack it here).",
        "",
        "Included:",
    ]
    for item in included:
        lines.append(f"  + {item}")
    lines.append("")
    lines.append("Not included:")
    for item in excluded:
        lines.append(f"  - {item}")
    if notes:
        lines.append("")
        lines.append("Notes:")
        for item in notes:
            lines.append(f"  note: {item}")
    lines.extend(
        [
            "",
            "Restore:",
            "  docker compose stop core",
            f"  ./forgesre restore data/backups/backup_{stamp} --yes",
            "  ./forgesre update",
            "",
        ]
    )
    dest = run_dir / "MANIFEST.txt"
    dest.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def _run_dir_for_stamp(backup_dir: Path, stamp: str) -> Path:
    run_dir = backup_dir / f"backup_{stamp}"
    if not run_dir.exists():
        return run_dir
    return backup_dir / f"backup_{stamp_utc()}"


def create_backup(
    *,
    include_secrets: bool = True,
    include_models: bool = False,
    layout: BackupLayout | None = None,
) -> BackupResult:
    lay = layout or layout_from_env()
    ensure_backup_dir(lay)
    stamp = stamp_utc()
    run_dir = _run_dir_for_stamp(lay.backup_dir, stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    stamp = run_dir.name[len("backup_") :]
    included: list[str] = []
    excluded: list[str] = list(EXCLUDED_ALWAYS)
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="forgesre-bak-") as tmp:
        staging = Path(tmp) / f"forgesre-{stamp}"
        staging.mkdir()

        db_ok = False
        try:
            payload = dump_database()
            db_file = staging / "db.json"
            db_file.write_text(json.dumps(payload, indent=0), encoding="utf-8")
            try:
                os.chmod(db_file, 0o600)
            except OSError:
                pass
            included.append(
                "logical database dump (users, incidents, playbooks, playrules, journal, audit, jobs)"
            )
            db_ok = True
        except Exception as exc:  # noqa: BLE001 — still capture files if DB is down
            notes.append(f"database dump failed: {exc}")
            excluded.append("Database dump (connection failed)")

        if _copy_file(lay.config_yml, staging / "config" / "forgesre.yml"):
            included.append("config/forgesre.yml")
        else:
            notes.append("config/forgesre.yml missing")

        if _copy_file(lay.dotenv, staging / "dotenv"):
            included.append(".env (compose ports, FORGESRE_DATA, profiles)")
        else:
            notes.append(".env missing — restore of compose settings will be incomplete")

        if include_secrets:
            if _copy_file(lay.secrets, staging / "secrets.env"):
                included.append("secrets/secrets.env (admin-only archive)")
            else:
                notes.append("secrets/secrets.env missing")
        else:
            excluded.append("secrets/secrets.env omitted (--no-secrets)")
            notes.append("Archive has no secrets.env. Keep a copy elsewhere or restore will not log in.")

        local_alerts = lay.monitoring_dir / "alerts.local.yml"
        if _copy_file(local_alerts, staging / "monitoring" / "alerts.local.yml"):
            included.append("monitoring/alerts.local.yml")
        else:
            notes.append("no monitoring/alerts.local.yml (bundled alerts.yml is in git)")

        gen_files = _copy_tree(lay.generated_dir, staging / "generated", lambda _p: False)
        if gen_files:
            included.append(f"data/generated/ ({len(gen_files)} file(s); can be rewritten by render-monitoring)")
        else:
            notes.append("no data/generated yet — run ./forgesre render-monitoring after restore")

        log_files = _copy_tree(
            lay.logs_dir, staging / "logs", lambda path: path.stat().st_size >= LOG_FILE_MAX
        )
        if log_files:
            included.append(f"compressed logs ({len(log_files)} file(s) under data/logs)")
        if lay.logs_dir.is_dir():
            for path in lay.logs_dir.rglob("*"):
                if path.is_file() and path.stat().st_size >= LOG_FILE_MAX:
                    excluded.append(f"log {path.name} (>= {LOG_FILE_MAX} bytes)")

        def model_skip(path: Path) -> bool:
            if include_models:
                return False
            return path.suffix.lower() == ".gguf" or path.stat().st_size >= MODEL_SMALL_MAX

        model_files = _copy_tree(lay.models_dir, staging / "models", model_skip)
        skipped_models = []
        if lay.models_dir.is_dir() and not include_models:
            for path in lay.models_dir.rglob("*"):
                if path.is_file() and (path.suffix.lower() == ".gguf" or path.stat().st_size >= MODEL_SMALL_MAX):
                    skipped_models.append(path.name)
        if model_files:
            included.append(f"data/models/ ({len(model_files)} file(s); large GGUF skipped unless requested)")
        if skipped_models:
            excluded.append("LLM GGUF / large model files: " + ", ".join(skipped_models[:8]))
            notes.append("Re-fetch with ./forgesre fetch-llm, or backup with --include-models / the UI checkbox.")
        elif not model_files:
            notes.append("no data/models files (ForgeRCA works without a GGUF)")

        if lay.examples_dir.is_dir():
            _copy_tree(lay.examples_dir, staging / "examples", lambda _p: False)
            included.append("config/examples/ (file-based playbook/playrule samples; live rows are in the DB dump)")

        if _copy_file(lay.install_report, staging / "installation-report.md"):
            included.append("installation-report.md")

        manifest = {
            "format": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "stamp": stamp,
            "include_secrets": include_secrets,
            "include_models": include_models,
            "included": included,
            "excluded": excluded,
            "notes": notes,
            "db": db_ok,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (staging / "README-RESTORE.txt").write_text(_restore_readme(stamp, include_secrets, db_ok), encoding="utf-8")

        dest = run_dir / INNER_ARCHIVE
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(staging, arcname=f"forgesre-{stamp}")
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        _write_run_manifest(run_dir, stamp, included, excluded, notes)

    return BackupResult(
        path=dest,
        name=run_dir.name,
        included=included,
        excluded=excluded,
        notes=notes,
        secrets=include_secrets,
        db=db_ok,
    )


def _restore_readme(stamp: str, secrets: bool, db_ok: bool) -> str:
    secrets_line = "included" if secrets else "OMITTED — copy secrets/secrets.env from elsewhere first"
    db_line = "logical dump in db.json" if db_ok else "MISSING — archive has files only"
    return f"""ForgeSRE platform backup {stamp}

This archive is admin-only. Mode 600. Never commit it. Never serve it without a login.

Contains: config, .env, secrets ({secrets_line}), database ({db_line}),
logs, generated monitoring YAML, optional small models, example play YAML.

Does not contain: container images, Prometheus/Loki/Grafana volumes, nested backups.
GGUF weights are skipped unless this backup was made with --include-models.

Restore (does not run itself — you confirm):

  ssh you@forgesre-vm
  cd ~/forgesre
  docker compose stop core
  ./forgesre restore data/backups/backup_{stamp} --yes
  ./forgesre update

./forgesre restore without --yes only prints the plan and exits 1.
The Administration Import button stores an archive; Restore requires typing RESTORE.
Do not restore onto a live box you still need — it overwrites users, incidents, and secrets.
"""


def _archive_root(tar: tarfile.TarFile) -> str:
    members = [m.name for m in tar.getmembers()]
    if not members:
        raise ValueError("empty archive")
    return members[0].split("/")[0]


def read_manifest(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tar:
        root = _archive_root(tar)
        member = tar.getmember(f"{root}/manifest.json")
        payload = tar.extractfile(member)
        if payload is None:
            return {}
        return json.loads(payload.read().decode("utf-8"))


def inspect_archive(archive: Path, layout: BackupLayout | None = None) -> RestorePlan:
    lay = layout or layout_from_env()
    manifest = read_manifest(archive)
    return RestorePlan(
        archive=archive,
        included=list(manifest.get("included") or []),
        excluded=list(manifest.get("excluded") or []),
        notes=list(manifest.get("notes") or []),
        has_secrets=bool(manifest.get("include_secrets")),
        has_db=bool(manifest.get("db")),
        files_writable=lay.files_writable,
    )


def print_restore_plan(plan: RestorePlan) -> None:
    print(f"Archive: {plan.archive}")
    print("Would overwrite:")
    print("  PostgreSQL/SQLite (users, incidents, playbooks, playrules, journal, audit)")
    if plan.files_writable:
        print("  .env, secrets/secrets.env, config/forgesre.yml, alerts.local.yml, data/generated, logs")
    else:
        print("  Files on disk: NOT writable from this process (Core container mounts them read-only).")
        print("  Use SSH: docker compose stop core && ./forgesre restore ARCHIVE --yes && ./forgesre update")
    print("Included:")
    for item in plan.included:
        print(f"  - {item}")
    print("Not in this backup:")
    for item in plan.excluded:
        print(f"  - {item}")
    if plan.notes:
        print("Notes:")
        for item in plan.notes:
            print(f"  - {item}")
    print()
    print("This does not run silently. Pass --yes (CLI) or type RESTORE (UI).")
    print("After a host restore: ./forgesre update")


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest not in target.parents and target != dest:
            raise ValueError("archive member escapes extract dir")
        if member.issym() or member.islnk():
            raise ValueError("refusing archive with symlinks")
    tar.extractall(dest, filter="data")


def _stop_core() -> str:
    if os.environ.get("FORGESRE_RESTORE_STOP_CORE", "1").lower() in {"0", "false", "no"}:
        return "compose stop skipped (FORGESRE_RESTORE_STOP_CORE=0)"
    root = _repo_root()
    try:
        result = subprocess.run(
            ["docker", "compose", "stop", "core"],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            extra = (result.stderr or result.stdout or str(result.returncode)).strip()
            return f"docker compose stop core: {extra}"
        return "stopped compose service core"
    except Exception as exc:  # noqa: BLE001
        return f"could not stop core: {exc}"


def restore_archive(
    archive: Path,
    *,
    confirm: str = "",
    yes: bool = False,
    apply_files: bool | None = None,
    stop_core: bool = False,
    layout: BackupLayout | None = None,
) -> dict[str, Any]:
    """Apply a backup. Refuses unless yes=True or confirm==RESTORE."""
    if not yes and confirm.strip() != CONFIRM_WORD:
        raise PermissionError("restore refused without --yes or typed RESTORE")
    archive = archive_file(Path(archive))
    lay = layout or layout_from_env()
    plan = inspect_archive(archive, lay)
    notes: list[str] = []
    if stop_core:
        notes.append(_stop_core())
    apply = lay.files_writable if apply_files is None else apply_files
    with tempfile.TemporaryDirectory(prefix="forgesre-rst-") as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, tmp_path)
        roots = [p for p in tmp_path.iterdir() if p.is_dir()]
        unpacked = roots[0] if roots else tmp_path
        db_path = unpacked / "db.json"
        if db_path.is_file():
            payload = json.loads(db_path.read_text(encoding="utf-8"))
            restore_database(payload)
            notes.append("database restored")
        else:
            notes.append("no db.json in archive")
        if apply:
            mapping = [
                (unpacked / "dotenv", lay.dotenv),
                (unpacked / "secrets.env", lay.secrets),
                (unpacked / "config" / "forgesre.yml", lay.config_yml),
                (unpacked / "monitoring" / "alerts.local.yml", lay.monitoring_dir / "alerts.local.yml"),
                (unpacked / "installation-report.md", lay.install_report),
            ]
            for src, dest in mapping:
                if src.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    if dest.name in {".env", "secrets.env", "forgesre.yml"} or dest == lay.secrets:
                        try:
                            os.chmod(dest, 0o600)
                        except OSError:
                            pass
                    notes.append(f"wrote {dest}")
            for label, src, dest in (
                ("generated", unpacked / "generated", lay.generated_dir),
                ("logs", unpacked / "logs", lay.logs_dir),
                ("models", unpacked / "models", lay.models_dir),
            ):
                if src.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    for path in src.rglob("*"):
                        if path.is_file():
                            target = dest / path.relative_to(src)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(path, target)
                    notes.append(f"restored {label} files")
        else:
            notes.append(
                "skipped file restore (paths not writable from Core; "
                "use ./forgesre restore --yes on the VM)"
            )
    return {"ok": True, "archive": str(archive), "notes": notes, "plan": plan}


def save_upload(data: bytes, filename: str, layout: BackupLayout | None = None) -> Path:
    lay = layout or layout_from_env()
    ensure_backup_dir(lay)
    name = Path(filename or "").name
    stamp = stamp_utc()
    if ARCHIVE_RE.match(name):
        stamp = name[len("forgesre-") : -len(".tar.gz")]
    run_dir = _run_dir_for_stamp(lay.backup_dir, stamp)
    stamp = run_dir.name[len("backup_") :]
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    dest = run_dir / INNER_ARCHIVE
    dest.write_bytes(data)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    try:
        with tarfile.open(dest, "r:gz") as tar:
            root = _archive_root(tar)
            tar.getmember(f"{root}/manifest.json")
    except Exception:
        dest.unlink(missing_ok=True)
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise ValueError("not a ForgeSRE backup tar.gz (missing manifest.json)") from None
    try:
        manifest = read_manifest(dest)
        _write_run_manifest(
            run_dir,
            stamp,
            list(manifest.get("included") or []),
            list(manifest.get("excluded") or []),
            list(manifest.get("notes") or []),
        )
    except Exception:  # noqa: BLE001 — listing is optional
        listing = run_dir / "MANIFEST.txt"
        listing.write_text(
            f"Imported {name}\nArchive: {INNER_ARCHIVE}\n",
            encoding="utf-8",
        )
        try:
            os.chmod(listing, 0o600)
        except OSError:
            pass
    return dest


def format_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KiB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MiB"
    return f"{num / (1024 * 1024 * 1024):.2f} GiB"


def main(argv: list[str] | None = None) -> int:
    import sys

    try:
        return _main(argv)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("Backup cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — host CLI must not print a traceback
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "help"
    if cmd in {"-h", "--help", "help"}:
        sys.stdout.write(
            "backup|restore|import|remove|list  "
            "(see ./forgesre help backup, restore, remove)\n"
        )
        return 0
    if cmd == "list":
        rows = list_archives()
        if not rows:
            print("No backups under data/backups/.")
            return 0
        for index, row in enumerate(rows, 1):
            print(f"{index}. {row['label']}")
        return 0
    if cmd == "backup":
        include_secrets = "--no-secrets" not in args
        include_models = "--include-models" in args
        result = create_backup(include_secrets=include_secrets, include_models=include_models)
        print(f"Wrote {result.path} (mode 600)")
        for item in result.included:
            print(f"  + {item}")
        for item in result.excluded:
            print(f"  - {item}")
        for item in result.notes:
            print(f"  note: {item}")
        if not result.db:
            print(
                "ERROR: database dump failed. Archive has files only. "
                "Start Postgres: docker compose up -d postgres",
                file=sys.stderr,
            )
            return 1
        return 0
    if cmd in {"restore", "import"}:
        yes = "--yes" in args
        args = [a for a in args if a not in {"--yes", "--include-models", "--no-secrets"}]
        args = _pop_backup_noun(args)
        confirm = ""
        if "--confirm" in args:
            idx = args.index("--confirm")
            if idx + 1 < len(args):
                confirm = args[idx + 1]
                del args[idx : idx + 2]
        archive_arg = next((a for a in args if not a.startswith("-")), "")
        rows = list_archives()
        if not archive_arg:
            print_backup_picker(rows)
            if not rows:
                return 1
            archive_arg = read_picker_choice()
            if not archive_arg:
                print("Pick a number, then: ./forgesre restore N [--yes]")
                print("Or: ./forgesre import backup   then pick a number")
                print("Or pass a folder: ./forgesre restore data/backups/backup_YYYYMMDDTHHMMSSZ [--yes]")
                print("Still needs --yes (or type RESTORE in Administration).")
                return 1
        try:
            path = resolve_cli_archive(archive_arg, rows=rows)
        except (ValueError, FileNotFoundError) as exc:
            print(exc)
            return 1
        plan = inspect_archive(path)
        print_restore_plan(plan)
        if not yes and confirm != CONFIRM_WORD:
            ident = backup_ident(path)
            print("Refusing. Re-run with --yes after you have stopped Core (or accept a live DB restore).")
            print("  docker compose stop core")
            print(f"  ./forgesre restore {ident} --yes")
            print("  ./forgesre update")
            return 1
        outcome = restore_archive(path, yes=True, stop_core=True)
        for item in outcome["notes"]:
            print(item)
        print("Next: ./forgesre update")
        return 0
    if cmd == "remove":
        yes = "--yes" in args
        args = [a for a in args if a not in {"--yes", "--include-models", "--no-secrets"}]
        args = _pop_backup_noun(args)
        target_arg = next((a for a in args if not a.startswith("-")), "")
        rows = list_archives()
        if not target_arg:
            print_backup_picker(rows)
            if not rows:
                return 1
            print("Remove deletes that one folder (tar + MANIFEST) only — not data/ or data/backups/.")
            target_arg = read_picker_choice()
            if not target_arg:
                print("Pick a number, then: ./forgesre remove backup N [--yes]")
                print("Confirm with --yes (or type yes when asked).")
                return 1
        try:
            path = resolve_cli_archive(target_arg, rows=rows)
        except (ValueError, FileNotFoundError) as exc:
            print(exc)
            return 1
        ident = backup_ident(path)
        print(f"Would remove {ident} (that run folder or legacy tar only).")
        if not yes:
            if read_yes_confirm():
                yes = True
            else:
                print("Refusing. Re-run with --yes after you pick the folder:")
                print(f"  ./forgesre remove backup {ident} --yes")
                return 1
        deleted = delete_backup(ident)
        print(f"Removed {deleted.name}")
        return 0
    print(f"unknown backup subcommand: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
