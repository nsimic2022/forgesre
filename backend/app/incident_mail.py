"""Send the incident report by mail from the new incident page.

``mail_draft`` gives the subject, the generated report text and address suggestions (asset owner
first, then earlier recipients of this incident, then the address book). ``queue_mail`` checks the
addresses and queues the ``incident_mail`` job; the job sends one mail per address through
``send_incident_report`` (SMTP has its own timeout), so the web request never waits on SMTP.
Every send is an outbox row with status sent, failed or generated (SMTP off).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Incident, Notification

MAIL_KIND = "incident_mail"
MAX_TO = 10


def _split(raw: str | list[str]) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").split(",")
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
    return out


def mail_draft(db: Session, incident: Incident) -> dict[str, Any]:
    """Subject, report text and suggested addresses for the compose box. Sends nothing."""
    from app.settings import settings
    from app.incident_report_mail import build_incident_report
    from app.services import _valid_email, incident_alarm_subject, list_mail_addresses

    suggestions: list[dict[str, str]] = []

    def add(email: str, label: str) -> None:
        if _valid_email(email) and email.lower() not in {s["email"].lower() for s in suggestions}:
            suggestions.append({"email": email.strip(), "label": label})

    asset = incident.asset
    if asset is not None and asset.owner_email:
        add(asset.owner_email, f"Owner of {asset.hostname}")
    earlier = (
        db.query(Notification.target)
        .filter(Notification.incident_id == incident.id, Notification.step_key == "incident-report")
        .order_by(Notification.id.desc())
        .all()
    )
    for (target,) in earlier:
        add(str(target or ""), "Got this report before")
    for row in list_mail_addresses(db)[:50]:
        add(row["email"], row.get("label") or row.get("source") or "")
    sent = [
        {"to": n.target, "status": n.status, "at": n.created_at.isoformat() if n.created_at else None}
        for n in db.query(Notification)
        .filter(Notification.incident_id == incident.id, Notification.step_key == "incident-report")
        .order_by(Notification.id.desc())
        .limit(10)
    ]
    return {
        "subject": incident_alarm_subject(incident),
        "body": build_incident_report(db, incident),
        "suggestions": suggestions,
        "sent": sent,
        "smtp": bool(settings.email_enabled and settings.smtp_host),
    }


def queue_mail(db: Session, incident: Incident, to: str | list[str], subject: str, body: str, actor: str):
    """Check the addresses and queue one incident_mail job. Raises ValueError with a plain reason."""
    from app.jobs import enqueue
    from app.services import _valid_email

    addresses = _split(to)
    if not addresses:
        raise ValueError("Give at least one email address.")
    if len(addresses) > MAX_TO:
        raise ValueError(f"At most {MAX_TO} addresses at once.")
    bad = [a for a in addresses if not _valid_email(a)]
    if bad:
        raise ValueError(f"Not an email address: {', '.join(bad)}")
    return enqueue(db, MAIL_KIND, f"{incident.number}#{uuid4().hex[:8]}", object_type="incident",
                   payload={"to": addresses, "subject": subject or "", "body": body or "", "actor": actor})


def run_mail(db: Session, number: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    """Job body: one incident report per address. An edited body goes as text only; the generated one also as HTML."""
    from app.services import send_incident_report

    incident = db.query(Incident).filter_by(number=number).first()
    if incident is None:
        raise RuntimeError(f"incident {number} not found")
    results: list[dict[str, str]] = []
    for address in payload.get("to") or []:
        try:
            row = send_incident_report(db, incident, address, actor=str(payload.get("actor") or "system"),
                                       subject=payload.get("subject") or None, body=payload.get("body") or None)
            results.append({"to": address, "status": row.status, "error": row.error or ""})
        except ValueError as exc:
            results.append({"to": address, "status": "failed", "error": str(exc)})
    return results
