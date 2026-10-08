"""Background jobs. Postgres durability, no Redis/Celery."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.journal import report
from app.models import Incident, Job, utcnow

log = logging.getLogger("forgesre")

DISCOVERY_SCAN_KIND = "discovery_scan"
DISCOVERY_SCAN_OBJECT_ID = "scan"
# Re-running these is safe: builtin RCA returns the existing result, an LLM rewrite only adds
# a new Investigation row, a discovery scan upserts candidates. None of them sends mail.
RESTART_RETRY_KINDS = ("investigate", DISCOVERY_SCAN_KIND)
RESTART_MAX_ATTEMPTS = 3
INTERRUPTED_NOTE = "interrupted by Core restart"


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


def active_discovery_scan(db: Session) -> Job | None:
    """Pending or running discovery probe, if any."""
    return (
        db.query(Job)
        .filter(
            Job.kind == DISCOVERY_SCAN_KIND,
            Job.object_id == DISCOVERY_SCAN_OBJECT_ID,
            Job.status.in_(["pending", "running"]),
        )
        .order_by(Job.id.desc())
        .first()
    )


def enqueue_discovery_scan(
    db: Session,
    *,
    actor: str = "system",
    cidrs: list[str] | None = None,
    saved: bool = False,
) -> Job | None:
    """Queue a discovery probe. Dedupes while one is pending/running. Does not probe here."""
    payload: dict = {"actor": actor, "saved": saved}
    if cidrs is not None:
        payload["cidrs"] = list(cidrs)
    return enqueue(
        db,
        DISCOVERY_SCAN_KIND,
        DISCOVERY_SCAN_OBJECT_ID,
        object_type="discovery",
        payload=payload,
    )


def recover_interrupted_jobs(db: Session) -> dict[str, int]:
    """Core startup, before the jobs loop: a `running` row belonged to the old process and nobody will finish it.

    Retryable kinds go back to pending (until RESTART_MAX_ATTEMPTS); the rest become error so a new
    job can queue. Either way the row stops blocking enqueue() and the "LLM pending" pill.
    """
    rows = db.query(Job).filter(Job.status == "running").order_by(Job.id).all()
    requeued = failed = 0
    for row in rows:
        if row.kind in RESTART_RETRY_KINDS and int(row.attempts or 0) < RESTART_MAX_ATTEMPTS:
            row.status = "pending"
            row.started_at = None
            row.error = f"{INTERRUPTED_NOTE}; queued again"
            requeued += 1
        else:
            row.status = "error"
            row.finished_at = utcnow()
            row.error = f"{INTERRUPTED_NOTE}; not retried (attempt {int(row.attempts or 0)})"
            failed += 1
    if rows:
        db.commit()
        log.warning("jobs: %d requeued, %d marked error after restart", requeued, failed)
        report(
            db,
            "jobs",
            "recover",
            "error" if failed else "ok",
            summary=f"{len(rows)} job(s) were running when Core stopped: {requeued} queued again, {failed} marked error",
            detail=", ".join(f"#{row.id} {row.kind} {row.object_id} → {row.status}" for row in rows)[:2000],
        )
    return {"requeued": requeued, "failed": failed}


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
            elif row.kind == DISCOVERY_SCAN_KIND:
                from app.inventory import run_scan

                # Payload cidrs = confirmed list (already persisted). Empty → no scan.
                payload = row.payload or {}
                cidrs = payload.get("cidrs")
                if isinstance(cidrs, list):
                    run_scan(db, cidrs=list(cidrs), merge_auto=False)
                else:
                    run_scan(db, merge_auto=False)
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
            module = "discovery" if row.kind == DISCOVERY_SCAN_KIND else "rca"
            action = "scan" if row.kind == DISCOVERY_SCAN_KIND else "job"
            report(
                db,
                module,
                action,
                "error",
                summary=f"Job {row.kind} failed for {row.object_id}",
                detail=str(exc),
                object_type=row.object_type,
                object_id=row.object_id,
            )
    return done


def list_jobs(db: Session, limit: int = 50) -> list[Job]:
    return db.query(Job).order_by(Job.id.desc()).limit(limit).all()
