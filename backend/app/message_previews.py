"""What ForgeSRE sends, rendered with the real builders but never sent.

Settings › Messages and Reports use this to show the alert mail, the chat message, the incident
report and a scheduled report exactly as a recipient gets them. Nothing here talks to SMTP or a
chat service; the scheduled report reads Prometheus with the same short timeouts as a real send.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Asset, Incident, ScheduledReport, utcnow

SAMPLE_NOTE = "Sample incident: there is no incident yet, so the preview uses made-up values."


def _sample_incident() -> Incident:
    """An unsaved incident with an asset, for an empty appliance. Never added to the session."""
    asset = Asset(
        asset_id="sample-db-01", hostname="db-ora-02", ip="10.10.30.2", type="Oracle DB",
        environment="Production", owner="Database team", contact_name="Ana Petrović",
        owner_email="dba@example.com", owner_phone="+381 60 123 4567", status="active",
    )
    return Incident(
        number="INC-0001", title="Disk /u02 92%", severity="HIGH", status="OPEN", source="prometheus",
        started_at=utcnow() - timedelta(minutes=4), asset=asset, summary="",
    )


def pick_incident(db: Session, number: str = "") -> tuple[Incident, bool]:
    """The named incident, else the newest one, else a sample. Returns (incident, is_sample)."""
    query = db.query(Incident).options(joinedload(Incident.asset))
    row = query.filter(Incident.number == number).first() if number else None
    row = row or query.order_by(Incident.started_at.desc()).first()
    return (row, False) if row is not None else (_sample_incident(), True)


def _ack_link(incident: Incident) -> str:
    from app.ack_links import ack_url

    if incident.id is None:
        from app.ack_links import public_url

        return f"{public_url()}/ack/sample" if public_url() else ""
    try:
        return ack_url(incident.id, "preview@example.com")
    except Exception:
        return ""


def alert_email(incident: Incident) -> dict[str, str]:
    """The first escalation mail (Tier 1, at once), with its one-click acknowledge link."""
    from app.notifications import build_escalation_body, build_escalation_html
    from app.services import incident_alarm_subject

    link = _ack_link(incident)
    return {
        "subject": incident_alarm_subject(incident),
        "text": build_escalation_body(incident, "immediate", "team", link),
        "html": build_escalation_html(incident, "immediate", "team", link),
    }


def chat_messages(incident: Incident) -> dict[str, Any]:
    """The chat text for each event and the JSON body each service gets."""
    from app import chat

    events = {}
    for event, detail in (("opened", ""), ("escalated", "Tier 2 · team-lead"), ("resolved", "")):
        text = chat.message_text(incident, event, detail)
        events[event] = {
            "text": text,
            "payloads": {channel: chat.payload(channel, text, incident=incident, event=event) for channel in chat.CHANNELS},
        }
    return {"events": events, "channels": {c: chat.LABELS[c] for c in chat.CHANNELS}, "active": chat.active_channels()}


def incident_report(db: Session, incident: Incident) -> dict[str, str]:
    from app.incident_report_mail import build_incident_report, build_incident_report_html
    from app.services import incident_alarm_subject

    session = db if incident.id is not None else None
    return {
        "subject": incident_alarm_subject(incident),
        "text": build_incident_report(session, incident) if session is not None else "",
        "html": build_incident_report_html(session, incident),
    }


def scheduled_report(db: Session, report_id: int | None = None) -> dict[str, Any]:
    """The scheduled report mail: the saved one when ``report_id`` is given, else a weekly sample."""
    from app.services import performance_report_message, report_kind, report_recipients, schedule_label

    row = db.get(ScheduledReport, report_id) if report_id else None
    if report_id and row is None:
        raise LookupError("report not found")
    if row is not None and report_kind(row) == "forgeai":
        from app.morning_digest import digest_facts, digest_message

        subject, text, html = digest_message(row.name, digest_facts(db, min(report_days(row), 7) * 24), None)
        return {
            "subject": subject, "html": html, "text": text,
            "note": "The facts are today's. ForgeAI writes its summary when the mail is sent, so it is not in the preview.",
            "recipients": report_recipients(row), "schedule": schedule_label(row),
        }
    name = row.name if row is not None else "weekly NOC"
    asset_ids = list(row.asset_ids or []) if row is not None else []
    days = report_days(row)
    subject, text, html = performance_report_message(db, asset_ids, name, days=days)
    return {
        "subject": subject, "text": text, "html": html, "note": "",
        "recipients": report_recipients(row) if row is not None else [],
        "schedule": schedule_label(row) if row is not None else "Every Monday 08:00 (sample)",
    }


def report_days(row: ScheduledReport | None) -> int:
    """How far back a report looks: one schedule interval, 1 to 31 days (7 when unknown).

    Daily → 1, weekly → 7, monthly → 30; a cron line is read by its day-of-month and weekday fields.
    """
    from app.services import report_schedule

    if row is None:
        return 7
    sch = report_schedule(row)
    cron = str(sch.get("cron") or "").split()
    if len(cron) == 5:
        dom, dow = cron[2], cron[4]
        if dom != "*":
            return 30
        if dow != "*" and "," not in dow and "-" not in dow:
            return 7
        return 1
    minutes = int(sch.get("every_minutes") or 0)
    if minutes <= 0:
        return 7
    return max(1, min(31, round(minutes / 1440)))
