"""Platform backup and restore. Same code for ./forgesre backup|restore and /admin.

Archives live under data/backups/ (gitignored, mode 700/600). They include
config, .env, secrets (unless omitted), a logical DB dump (users, incidents,
playbooks, playrules, journal, …), compressed logs, generated monitoring files,
and alerts.local.yml. They do not include Docker images, Prometheus/Loki/Grafana
TSDB volumes, or multi-GB GGUF weights unless the operator opts in.
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

from sqlalchemy import DateTime as SADateTime
from sqlalchemy import text

ARCHIVE_RE = re.compile(r"^forgesre-\d{8}T\d{6}Z\.tar\.gz$")
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
    """Resolve host or Core-container paths. Extra env vars are set in compose."""
    from app.settings import settings

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
        config_yml=Path(os.environ.get("FORGESRE_CONFIG") or settings.config_path),
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


def is_archive_name(name: str) -> bool:
    return bool(ARCHIVE_RE.match(Path(name).name))


def resolve_archive(name: str, layout: BackupLayout | None = None) -> Path:
    lay = layout or layout_from_env()
    base = ensure_backup_dir(lay).resolve()
    raw = Path(name).name
    if not is_archive_name(raw):
        raise ValueError("not a ForgeSRE backup archive name")
    path = (base / raw).resolve()
    if base not in path.parents and path != base:
        raise ValueError("backup path escapes data/backups")
    if not path.is_file():
        raise FileNotFoundError(raw)
    return path


def list_archives(layout: BackupLayout | None = None) -> list[dict[str, Any]]:
    lay = layout or layout_from_env()
    folder = ensure_backup_dir(lay)
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob("forgesre-*.tar.gz"), key=lambda item: item.name, reverse=True):
        if not path.is_file() or not is_archive_name(path.name):
            continue
        stat = path.stat()
        rows.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return rows


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def dump_database() -> dict[str, list[dict[str, Any]]]:
    from app.db import Base, engine

    payload: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(table.select()).mappings().all()
            payload[table.name] = [{k: _jsonable(v) for k, v in dict(row).items()} for row in rows]
    return payload


def _coerce_row(table: Any, row: dict[str, Any]) -> dict[str, Any]:
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


def restore_database(payload: dict[str, list[dict[str, Any]]]) -> None:
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


def create_backup(
    *,
    include_secrets: bool = True,
    include_models: bool = False,
    layout: BackupLayout | None = None,
) -> BackupResult:
    lay = layout or layout_from_env()
    ensure_backup_dir(lay)
    stamp = stamp_utc()
    included: list[str] = []
    excluded: list[str] = list(EXCLUDED_ALWAYS)
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="forgesre-bak-", dir=str(lay.backup_dir)) as tmp:
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

        dest = lay.backup_dir / f"forgesre-{stamp}.tar.gz"
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(staging, arcname=f"forgesre-{stamp}")
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass

    return BackupResult(
        path=dest,
        name=dest.name,
        included=included,
        excluded=excluded,
        notes=notes,
        secrets=include_secrets,
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
  ./forgesre restore data/backups/forgesre-{stamp}.tar.gz --yes
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
    if not is_archive_name(name):
        name = f"forgesre-{stamp_utc()}.tar.gz"
    dest = lay.backup_dir / name
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
        raise ValueError("not a ForgeSRE backup tar.gz (missing manifest.json)") from None
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

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "help"
    if cmd in {"-h", "--help", "help"}:
        sys.stdout.write(
            "backup|restore|list  (see ./forgesre help backup and ./forgesre help restore)\n"
        )
        return 0
    if cmd == "list":
        for row in list_archives():
            print(f"{row['name']}  {format_size(row['size'])}")
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
        return 0
    if cmd == "restore":
        yes = "--yes" in args
        args = [a for a in args if a not in {"--yes", "--include-models", "--no-secrets"}]
        confirm = ""
        if "--confirm" in args:
            idx = args.index("--confirm")
            if idx + 1 < len(args):
                confirm = args[idx + 1]
                del args[idx : idx + 2]
        archive_arg = next((a for a in args if not a.startswith("-")), "")
        if not archive_arg:
            print("usage: ./forgesre restore ARCHIVE.tar.gz [--yes]")
            return 1
        path = Path(archive_arg)
        if not path.is_file():
            try:
                path = resolve_archive(path.name)
            except (ValueError, FileNotFoundError) as exc:
                print(exc)
                return 1
        plan = inspect_archive(path)
        print_restore_plan(plan)
        if not yes and confirm != CONFIRM_WORD:
            print("Refusing. Re-run with --yes after you have stopped Core (or accept a live DB restore).")
            return 1
        outcome = restore_archive(path, yes=True, stop_core=True)
        for item in outcome["notes"]:
            print(item)
        print("Next: ./forgesre update")
        return 0
    print(f"unknown backup subcommand: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
