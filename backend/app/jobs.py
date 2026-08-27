"""Background jobs. Postgres durability, no Redis/Celery."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.journal import report
from app.models import Incident, Job, utcnow

log = logging.getLogger("forgesre")


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


def job_is_llm(row: Job) -> bool:
    """True when this investigate job will wait on llama.cpp."""
    if row.kind != "investigate":
        return False
    payload = row.payload or {}
    return payload.get("use_llm", True) is not False


def next_pending_job(db: Session) -> Job | None:
    """FIFO among pending jobs, but builtin RCA (use_llm=false) before LLM rewrite."""
    pending = db.query(Job).filter_by(status="pending").order_by(Job.id).limit(32).all()
    if not pending:
        return None
    for row in pending:
        if not job_is_llm(row):
            return row
    return pending[0]


def run_pending_jobs(db: Session, limit: int = 8) -> int:
    """Claim and run pending jobs. Safe for sqlite tests (single-threaded)."""
    from app.services import queue_llm_rewrite, run_investigation

    done = 0
    for _ in range(limit):
        row = next_pending_job(db)
        if row is None:
            break
        if job_is_llm(row) and done > 0:
            # Leave the rewrite pending so the jobs loop can send /ops reports first.
            break
        row.status = "running"
        row.started_at = utcnow()
        row.attempts = int(row.attempts or 0) + 1
        db.commit()
        try:
            use_llm = False
            incident = None
            if row.kind == "investigate":
                incident = db.query(Incident).filter_by(number=row.object_id).first()
                if incident is None:
                    raise RuntimeError(f"incident {row.object_id} not found")
                payload = row.payload or {}
                use_llm = payload.get("use_llm", True) is not False
                run_investigation(
                    db,
                    incident,
                    actor=str(payload.get("actor") or "system"),
                    force=bool(payload.get("force")),
                    use_llm=use_llm,
                )
            else:
                raise RuntimeError(f"unknown job kind {row.kind}")
            row.status = "done"
            row.finished_at = utcnow()
            row.error = ""
            db.commit()
            done += 1
            if row.kind == "investigate" and incident is not None and not use_llm:
                queue_llm_rewrite(
                    db,
                    incident,
                    actor=str((row.payload or {}).get("actor") or "system"),
                )
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
