"""Incident history: 90-day lists, mail outbox, audit, operator notes.

Reads existing Postgres tables. Does not replace Incidents / Escalation / Journal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, or_
from sqlalchemy.orm import Session, joinedload

from app.audit import audit
from app.models import Asset, AuditLog, Incident, IncidentNote, Notification

DEFAULT_DAYS = 90
MAX_DAYS = 3660
LIST_LIMIT = 200
NOTE_MAX = 4000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp_days(raw: Any, default: int = DEFAULT_DAYS) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = default
    return max(1, min(days, MAX_DAYS))


def cutoff_since(days: int) -> datetime:
    return utcnow() - timedelta(days=days)


def list_history(
    db: Session,
    *,
    days: int = DEFAULT_DAYS,
    status: str = "",
    asset: str = "",
    number: str = "",
    limit: int = LIST_LIMIT,
    offset: int = 0,
) -> tuple[list[Incident], int]:
    days = clamp_days(days)
    limit = max(1, min(int(limit or LIST_LIMIT), 500))
    offset = max(0, int(offset or 0))
    query = db.query(Incident).filter(Incident.started_at >= cutoff_since(days))
    status = (status or "").strip().upper()
    if status:
        query = query.filter(Incident.status == status)
    number = (number or "").strip()
    if number:
        query = query.filter(Incident.number.ilike(f"%{number}%"))
    asset = (asset or "").strip()
    if asset:
        needle = f"%{asset}%"
        query = query.filter(
            exists().where(
                Asset.id == Incident.asset_id,
                or_(
                    Asset.asset_id.ilike(needle),
                    Asset.hostname.ilike(needle),
                    Asset.ip.ilike(needle),
                ),
            )
        )
    total = query.count()
    rows = query.options(joinedload(Incident.asset)).order_by(Incident.id.desc()).offset(offset).limit(limit).all()
    return rows, total


def notifications_for(db: Session, incident: Incident) -> list[Notification]:
    return (
        db.query(Notification)
        .filter(Notification.incident_id == incident.id)
        .order_by(Notification.id.asc())
        .all()
    )


def reported_to_for(db: Session, incidents: list[Incident]) -> dict[int, str]:
    """Unique incident-report recipients, in send order."""
    ids = [row.id for row in incidents if getattr(row, "id", None)]
    if not ids:
        return {}
    rows = (
        db.query(Notification.incident_id, Notification.target)
        .filter(Notification.incident_id.in_(ids), Notification.step_key == "incident-report")
        .order_by(Notification.id.asc())
        .all()
    )
    grouped: dict[int, list[str]] = {}
    for incident_id, target in rows:
        text = str(target or "").strip()
        if not text:
            continue
        bucket = grouped.setdefault(int(incident_id), [])
        if text not in bucket:
            bucket.append(text)
    return {key: ", ".join(values) for key, values in grouped.items()}


def audit_for(db: Session, number: str) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.object_type == "incident", AuditLog.object_id == number)
        .order_by(AuditLog.id.asc())
        .all()
    )


def notes_for(db: Session, incident: Incident) -> list[IncidentNote]:
    return (
        db.query(IncidentNote)
        .filter(IncidentNote.incident_id == incident.id)
        .order_by(IncidentNote.id.asc())
        .all()
    )


def add_note(db: Session, incident: Incident, actor: str, body: str) -> IncidentNote:
    text = (body or "").strip()[:NOTE_MAX]
    if not text:
        raise ValueError("note is empty")
    row = IncidentNote(incident_id=incident.id, actor=actor, body=text)
    db.add(row)
    audit(
        db,
        "incident.note",
        actor=actor,
        object_type="incident",
        object_id=incident.number,
        data={"chars": len(text)},
    )
    db.commit()
    db.refresh(row)
    return row


def apply_status_fields(incident: Incident, status: str, actor: str) -> None:
    """Ack / resolve timestamps. Safe to call from UI and API."""
    now = utcnow()
    status = status.upper()
    if status == "INVESTIGATING" and not incident.ack_at:
        incident.ack_at = now
        incident.ack_by = actor
    if status in {"RESOLVED", "CLOSED"}:
        incident.ended_at = now
        incident.resolved_at = now
        incident.resolved_by = actor


def notification_as_dict(row: Notification) -> dict[str, Any]:
    return {
        "id": row.id,
        "target": row.target,
        "subject": row.subject,
        "body": row.body,
        "status": row.status,
        "step_key": row.step_key,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "channel": row.channel,
    }


def audit_as_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "action": row.action,
        "data": row.data or {},
    }


def note_as_dict(row: IncidentNote) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "body": row.body,
    }
