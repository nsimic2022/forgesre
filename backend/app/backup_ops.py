"""Backup checks and housekeeping run on the host: verify an archive, prune old local runs.

``verify`` proves a backup could be restored without touching the running platform: it reads
the whole tar.gz (a damaged file fails here), parses ``db.json``, checks the tables a restore
needs and that each has rows where the live platform must have them, and reports what else
is inside (config, secrets). ``prune`` keeps the newest ``FORGESRE_BACKUP_KEEP`` run folders
(default 14); only the scheduled backup calls it, so manual backups are never removed by hand
commands. Standard library only (host python3).
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
from pathlib import Path
from typing import Any

# A restore without these is not a working platform.
REQUIRED_TABLES = ("users", "incidents", "assets")
# These must not be empty in a real install (there is always at least the first Admin).
NON_EMPTY = ("users",)
STATUS_FILE = ".verify-status.json"


def verify(archive: Path) -> dict[str, Any]:
    """Read the whole archive and check db.json; returns ok, problems and a short summary."""
    problems: list[str] = []
    summary: dict[str, Any] = {"archive": str(archive), "size": archive.stat().st_size if archive.exists() else 0}
    if not archive.exists():
        return {"ok": False, "problems": ["archive not found"], **summary}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()  # walks the whole gzip stream: a cut or damaged file fails here
            root = members[0].name.split("/")[0] if members else ""
            names = {m.name[len(root) + 1:] for m in members if m.name.startswith(root + "/")}
            summary["files"] = len(members)
            summary["has_secrets"] = "secrets.env" in names
            summary["has_env"] = "dotenv" in names  # .env is stored as "dotenv"
            summary["has_config"] = any(n.startswith("config/") for n in names)
            db_member = next((m for m in members if m.name == f"{root}/db.json"), None)
            if db_member is None:
                problems.append("no db.json in the archive (database not backed up)")
                db: dict[str, Any] = {}
            else:
                handle = tar.extractfile(db_member)
                db = json.loads(handle.read().decode("utf-8")) if handle else {}
            for member in members:  # read every file body once, so a corrupt block anywhere fails
                if member.isfile() and member is not db_member:
                    body = tar.extractfile(member)
                    while body is not None and body.read(1 << 20):
                        pass
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        return {"ok": False, "problems": [f"archive cannot be read: {type(exc).__name__}: {exc}"[:300]], **summary}
    if db:
        if not isinstance(db, dict):
            problems.append("db.json is not a table → rows map")
            db = {}
        summary["tables"] = len(db)
        summary["rows"] = {t: len(db.get(t) or []) for t in ("users", "incidents", "assets", "playbooks", "audit_log") if t in db}
        for table in REQUIRED_TABLES:
            if table not in db:
                problems.append(f"table {table} missing from db.json")
        for table in NON_EMPTY:
            if table in db and not db[table]:
                problems.append(f"table {table} is empty")
    if not summary.get("has_config"):
        problems.append("config/ missing")
    return {"ok": not problems, "problems": problems, **summary}


def format_verify(result: dict[str, Any]) -> str:
    out = [f"{'OK' if result['ok'] else 'FAILED'}: {result['archive']} ({result.get('size', 0) // 1024} KB, {result.get('files', 0)} files)"]
    if result.get("tables") is not None:
        rows = ", ".join(f"{t} {n}" for t, n in (result.get("rows") or {}).items())
        out.append(f"  database: {result['tables']} tables; {rows}")
    out.append(f"  config: {'yes' if result.get('has_config') else 'NO'}, .env: {'yes' if result.get('has_env') else 'no'}, "
               f"secrets: {'yes' if result.get('has_secrets') else 'no (restore needs them from elsewhere)'}")
    for problem in result.get("problems") or []:
        out.append(f"  problem: {problem}")
    return "\n".join(out) + "\n"


def record(backup_dir: Path, result: dict[str, Any]) -> None:
    """Keep the last verify result for doctor."""
    from datetime import datetime, timezone

    try:
        (backup_dir / STATUS_FILE).write_text(json.dumps({**result, "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    except OSError:
        pass


def keep_count(env: dict[str, str] | None = None) -> int:
    raw = (os.environ if env is None else env).get("FORGESRE_BACKUP_KEEP") or "14"
    try:
        return max(1, min(365, int(raw)))
    except ValueError:
        return 14


def prune(backup_dir: Path, keep: int) -> list[str]:
    """Delete run folders beyond the newest ``keep`` (by folder name = UTC time). Returns removed names."""
    runs = sorted((p for p in backup_dir.iterdir() if p.is_dir() and p.name.startswith("backup_")), key=lambda p: p.name)
    removed = []
    for old in runs[: max(0, len(runs) - keep)]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed
