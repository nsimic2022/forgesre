"""Internal process journal: one short ok/error report per module action.

Raw container logs stay in Docker / Loki. This table is the operator console:
split by module, pruned automatically so search stays small.
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import JournalEntry, utcnow

log = logging.getLogger("forgesre")

MODULES = [
    "install",
    "core",
    "seed",
    "inventory",
    "discovery",
    "incident",
    "rca",
    "escalation",
    "notification",
    "demo",
    "netbox",
    "snmp",
    "jobs",
    "backup",
]

KEEP_PER_MODULE = 200
PRUNE_AFTER = 250
DETAIL_MAX = 4000


def report(
    db: Session,
    module: str,
    action: str,
    status: str = "ok",
    summary: str = "",
    detail: str = "",
    object_type: str = "",
    object_id: str = "",
    duration_ms: int = 0,
    commit: bool = True,
) -> JournalEntry | None:
    """Write a module report. Never raises — a journal failure must not break the action."""
    status = (status or "ok").lower()
    if status not in {"ok", "warn", "error"}:
        status = "ok"
    module = (module or "core").strip()[:64] or "core"
    action = (action or "").strip()[:64]
    summary = (summary or "")[:512]
    detail = (detail or "")[:DETAIL_MAX]
    try:
        row = JournalEntry(
            at=utcnow(),
            module=module,
            action=action,
            status=status,
            summary=summary,
            detail=detail,
            object_type=(object_type or "")[:64],
            object_id=str(object_id or "")[:64],
            duration_ms=int(duration_ms or 0),
        )
        db.add(row)
        if commit:
            db.commit()
            prune_module(db, module)
        level = {"ok": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(status, logging.INFO)
        log.log(
            level,
            "journal module=%s action=%s status=%s %s",
            module,
            action,
            status,
            summary or object_id,
        )
        return row
    except Exception:
        log.exception("journal write failed module=%s action=%s", module, action)
        if commit:
            try:
                db.rollback()
            except Exception:
                pass
        return None


def prune_module(db: Session, module: str, keep: int = KEEP_PER_MODULE) -> int:
    try:
        count = db.query(func.count(JournalEntry.id)).filter_by(module=module).scalar() or 0
        threshold = PRUNE_AFTER if keep == KEEP_PER_MODULE else keep
        if count <= threshold:
            return 0
        keep_ids = [
            row[0]
            for row in (
                db.query(JournalEntry.id)
                .filter_by(module=module)
                .order_by(JournalEntry.id.desc())
                .limit(keep)
                .all()
            )
        ]
        if not keep_ids:
            return 0
        deleted = (
            db.query(JournalEntry)
            .filter(JournalEntry.module == module, JournalEntry.id.notin_(keep_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted or 0)
    except Exception:
        log.exception("journal prune failed module=%s", module)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def error_banner_entries(db: Session, ack_id: int | None, limit: int = 5) -> list[JournalEntry]:
    """Recent error reports newer than the operator's last dismissed journal id."""
    rows = list_entries(db, status="error", limit=limit)
    threshold = int(ack_id or 0)
    return [row for row in rows if int(row.id or 0) > threshold]


def next_error_ack_id(until_id: int | None, current_ack: int | None, shown_ids: list[int]) -> int:
    """Advance the per-user watermark to the newest shown error, never past it."""
    shown_max = max((int(item) for item in shown_ids if item is not None), default=0)
    previous = int(current_ack or 0)
    if until_id is None:
        target = shown_max
    else:
        try:
            requested = int(until_id)
        except (TypeError, ValueError):
            requested = shown_max
        target = min(max(requested, 0), shown_max)
    return max(previous, target)


def list_entries(
    db: Session,
    module: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> list[JournalEntry]:
    query = db.query(JournalEntry)
    if module:
        query = query.filter(JournalEntry.module == module)
    if status:
        query = query.filter(JournalEntry.status == status)
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(
            (JournalEntry.summary.ilike(like))
            | (JournalEntry.detail.ilike(like))
            | (JournalEntry.action.ilike(like))
            | (JournalEntry.object_id.ilike(like))
        )
    return query.order_by(JournalEntry.id.desc()).limit(max(1, min(limit, 500))).all()


def module_counts(db: Session) -> list[dict]:
    rows = (
        db.query(JournalEntry.module, func.count(JournalEntry.id))
        .group_by(JournalEntry.module)
        .order_by(JournalEntry.module)
        .all()
    )
    counted = {name: int(total) for name, total in rows}
    return [{"module": name, "count": counted.get(name, 0)} for name in MODULES if counted.get(name, 0)] + [
        {"module": name, "count": counted[name]} for name in sorted(counted) if name not in MODULES
    ]


def entry_as_dict(row: JournalEntry) -> dict:
    return {
        "id": row.id,
        "at": row.at.isoformat() if row.at else None,
        "module": row.module,
        "action": row.action,
        "status": row.status,
        "summary": row.summary,
        "detail": row.detail,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "duration_ms": row.duration_ms,
    }
