"""Restore a backup from Settings › Backup.

A restore replaces the whole database, the jobs table included, so it cannot run as a normal job:
its own job row would be overwritten halfway. It runs in a background thread of the process that
got the request, and reports start and end in the journal (``backup.restore``), which every process
reads; the Backup panel polls it. Only one restore runs at a time per process, and a running one is
also visible to the others through its "started" journal line.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

_lock = threading.Lock()


def _run(path: Path, actor: str) -> None:
    from app.backup import CONFIRM_WORD, backup_ident, restore_archive
    from app.db import SessionLocal
    from app.journal import report

    ident = backup_ident(path)
    try:
        outcome = restore_archive(path, confirm=CONFIRM_WORD, stop_core=False)
        status, summary, detail = "ok", f"Restored {ident}", "\n".join(outcome.get("notes") or [])
    except Exception as exc:  # reported, never raised: there is no request waiting
        status, summary, detail = "error", f"Restore of {ident} failed", str(exc)[:2000]
    finally:
        _lock.release()
    db = SessionLocal()
    try:
        report(db, "backup", "restore", status, summary=summary, detail=f"by {actor}\n{detail}", object_type="backup", object_id=ident)
    finally:
        db.close()


def start_restore(path: Path, actor: str) -> bool:
    """Start the restore thread; False when one is already running here."""
    from app.backup import backup_ident
    from app.db import SessionLocal
    from app.journal import report

    if not _lock.acquire(blocking=False):
        return False
    db = SessionLocal()
    try:
        report(db, "backup", "restore", "warn", summary=f"Restoring {backup_ident(path)}…", detail=f"by {actor}",
               object_type="backup", object_id=backup_ident(path))
    finally:
        db.close()
    threading.Thread(target=_run, args=(path, actor), daemon=True, name="restore").start()
    return True


def restore_state(db: Session) -> dict[str, Any] | None:
    """The latest restore line from the journal: running (warn), ok or error, with when and what."""
    from app.models import JournalEntry

    row = db.query(JournalEntry).filter_by(module="backup", action="restore").order_by(JournalEntry.id.desc()).first()
    if row is None:
        return None
    return {"status": "running" if row.status == "warn" else row.status, "summary": row.summary,
            "detail": row.detail, "at": row.at.isoformat() if row.at else None}
