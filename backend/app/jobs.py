"""Background jobs. Postgres durability, no Redis/Celery."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.journal import report
from app.models import Incident, Job

log = logging.getLogger("forgesre")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, kind: str, object_id: str, object_type: str = "incident", payload: dict | None = None) -> Job | None:
    existing = (
        db.query(Job)
        .filter(
            Job.kind == kind,
            Job.object_id == object_id,
            Job.status.in_(["pending", "running"]),
        )
        .first()
    )
    if existing:
        return existing
    row = Job(
        kind=kind,
        status="pending",
        object_type=object_type,
        object_id=object_id,
        payload=payload or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def run_pending_jobs(db: Session, limit: int = 8) -> int:
    """Claim and run pending jobs. Safe for sqlite tests (single-threaded)."""
    from app.services import run_investigation

    done = 0
    for _ in range(limit):
        row = db.query(Job).filter_by(status="pending").order_by(Job.id).first()
        if row is None:
            break
        row.status = "running"
        row.started_at = utcnow()
        row.attempts = int(row.attempts or 0) + 1
        db.commit()
        try:
            if row.kind == "investigate":
                incident = db.query(Incident).filter_by(number=row.object_id).first()
                if incident is None:
                    raise RuntimeError(f"incident {row.object_id} not found")
                run_investigation(db, incident, actor=str((row.payload or {}).get("actor") or "system"))
            else:
                raise RuntimeError(f"unknown job kind {row.kind}")
            row.status = "done"
            row.finished_at = utcnow()
            row.error = ""
            db.commit()
            done += 1
        except Exception as exc:
            log.exception("job %s %s failed", row.kind, row.object_id)
            row.status = "error"
            row.finished_at = utcnow()
            row.error = str(exc)[:2000]
            db.commit()
            report(
                db,
                "rca",
                "job",
                "error",
                summary=f"Job {row.kind} failed for {row.object_id}",
                detail=str(exc),
                object_type=row.object_type,
                object_id=row.object_id,
            )
    return done


def list_jobs(db: Session, limit: int = 50) -> list[Job]:
    return db.query(Job).order_by(Job.id.desc()).limit(limit).all()
