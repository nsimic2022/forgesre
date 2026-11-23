"""Tickets: a GLPI ticket per incident (two-way) and an outbound webhook for other ticket systems.

GLPI (``tickets.glpi.enabled``, the GLPI URL and tokens of the inventory integration):
- "Open ticket" on an incident creates one GLPI Ticket in the background and stores its id and link;
- a sync job reads the ticket's status every 10 minutes and notes on the incident when it was
  solved or closed in GLPI (the incident is not closed by it: a person decides);
- when the incident resolves, a follow-up "Resolved in ForgeSRE" is added to the ticket.
Only Ticket and ITILFollowup are written; inventory in GLPI stays read-only (app.glpi).

Outbound webhook (``tickets.webhook.enabled`` + ``tickets.webhook.url``, Bearer secret
``TICKET_WEBHOOK_TOKEN``): one JSON POST per event (opened, escalated, resolved) so Jira,
ServiceNow, n8n or a script can create and update their own tickets. 10 s timeout, background job.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import Incident, Notification, utcnow
from app.settings import settings

log = logging.getLogger("forgesre")

CREATE_KIND = "ticket_create"
SYNC_KIND = "ticket_sync"
WEBHOOK_KIND = "ticket_webhook"
TIMEOUT = 10.0
URGENCY = {"CRITICAL": 5, "HIGH": 4, "WARNING": 3}  # GLPI: 5 very high … 1 very low
DONE_STATUSES = {5: "solved", 6: "closed"}
STATUS_NAMES = {1: "new", 2: "assigned", 3: "planned", 4: "waiting", 5: "solved", 6: "closed"}


def _cfg(name: str) -> dict[str, Any]:
    raw = ((settings.yaml.get("tickets") or {}).get(name) or {}) if isinstance(settings.yaml, dict) else {}
    return raw if isinstance(raw, dict) else {}


def glpi_ready() -> str:
    """Why GLPI tickets cannot be used, or "" when they can."""
    if not _cfg("glpi").get("enabled"):
        return "GLPI tickets are off (tickets.glpi.enabled)"
    if not settings.glpi_configured:
        return "GLPI is not set up: GLPI_URL, GLPI_APP_TOKEN, GLPI_USER_TOKEN"
    return ""


def _link(incident: Incident) -> str:
    base = (settings.yaml.get("system") or {}).get("public_url") or ""
    return f"{str(base).rstrip('/')}/app/incidents/{incident.number}" if base else ""


def _glpi_call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One GLPI call inside its own session (init, call, kill); raises on failure."""
    from app.glpi import _auth_headers, api_root, init_session, kill_session

    url, app, user = settings.glpi_url, settings.glpi_app_token, settings.glpi_user_token
    timeout = settings.glpi_timeout
    session = init_session(url, app, user, timeout=timeout, force=True)
    try:
        response = httpx.request(method, api_root(url) + path, json=body, headers=_auth_headers(app, user, session), timeout=TIMEOUT)
        if response.status_code >= 400:
            raise RuntimeError(f"GLPI {method} {path}: HTTP {response.status_code}")
        return response.json() if response.content else None
    finally:
        kill_session(url, app, session, timeout=timeout)


def ticket_view(incident: Incident) -> dict[str, Any] | None:
    if not incident.ticket_ref:
        return None
    return {"ref": incident.ticket_ref, "url": incident.ticket_url or "", "status": incident.ticket_status or ""}


def create_glpi_ticket(db: Session, incident: Incident, actor: str) -> dict[str, Any]:
    """Job: create the GLPI ticket for this incident (once) and store its id and link."""
    from app.audit import audit
    from app.services import append_timeline

    if incident.ticket_ref:
        return {"status": "exists", "ref": incident.ticket_ref}
    why = glpi_ready()
    if why:
        raise RuntimeError(why)
    host = incident.asset.hostname if incident.asset is not None else ""
    lines = [incident.summary or incident.title, "", f"ForgeSRE incident {incident.number.split('_')[0]}",
             f"Severity: {incident.severity}", f"Host: {host or '-'}"]
    if _link(incident):
        lines.append(_link(incident))
    created = _glpi_call("POST", "Ticket", {"input": {"name": f"[{incident.severity}] {incident.title}"[:250],
                                                      "content": "\n".join(lines), "urgency": URGENCY.get((incident.severity or "").upper(), 3),
                                                      "type": 1}})
    ticket_id = int((created or {}).get("id") or 0) if isinstance(created, dict) else 0
    if not ticket_id:
        raise RuntimeError("GLPI did not return a ticket id")
    base = settings.glpi_url.rstrip("/")
    incident.ticket_ref, incident.ticket_status = f"glpi:{ticket_id}", "new"
    incident.ticket_url = f"{base}/front/ticket.form.php?id={ticket_id}"
    append_timeline(incident, "ticket", "TICKET", f"GLPI ticket #{ticket_id} opened by {actor}")
    audit(db, "incident.ticket", actor=actor, object_type="incident", object_id=incident.number, data={"ticket": incident.ticket_ref})
    db.commit()
    return {"status": "created", "ref": incident.ticket_ref}


def sync_glpi_tickets(db: Session) -> dict[str, Any]:
    """Job: read the status of every open incident's GLPI ticket; note when GLPI solved or closed it."""
    from app.services import append_timeline, tracked_incident_statuses

    if glpi_ready():
        return {"status": "skipped"}
    rows = (db.query(Incident).filter(Incident.ticket_ref.like("glpi:%"), Incident.status.in_(tracked_incident_statuses()))
            .limit(100).all())
    changed = 0
    for inc in rows:
        try:
            data = _glpi_call("GET", f"Ticket/{inc.ticket_ref.split(':', 1)[1]}")
        except Exception as exc:  # noqa: BLE001 — one bad ticket must not stop the rest
            log.warning("GLPI ticket sync %s: %s", inc.ticket_ref, exc)
            continue
        status = STATUS_NAMES.get(int((data or {}).get("status") or 0), "") if isinstance(data, dict) else ""
        if status and status != inc.ticket_status:
            inc.ticket_status = status
            if int(data.get("status") or 0) in DONE_STATUSES:
                append_timeline(inc, f"ticket-{status}", "TICKET", f"GLPI ticket {status}; resolve the incident when it is fixed here too")
            changed += 1
    db.commit()
    return {"status": "ok", "checked": len(rows), "changed": changed}


def note_resolved(db: Session, incident: Incident) -> None:
    """Add a follow-up to the GLPI ticket when the incident resolves (best effort, in a job)."""
    from app.jobs import enqueue

    if incident.ticket_ref and incident.ticket_ref.startswith("glpi:") and not glpi_ready():
        enqueue(db, CREATE_KIND, f"followup:{incident.number}", object_type="incident",
                payload={"incident": incident.number, "followup": True})


def add_resolved_followup(db: Session, incident: Incident) -> dict[str, Any]:
    ticket_id = incident.ticket_ref.split(":", 1)[1]
    text = f"Resolved in ForgeSRE by {incident.resolved_by or 'the alert source'}."
    if incident.root_cause:
        text += f" Cause: {incident.root_cause}"
    _glpi_call("POST", f"Ticket/{ticket_id}/ITILFollowup", {"input": {"itemtype": "Ticket", "items_id": int(ticket_id), "content": text}})
    return {"status": "followup"}


def run_job(db: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Jobs worker for ticket_create (also used for the resolved follow-up)."""
    incident = db.query(Incident).filter_by(number=str(payload.get("incident") or "")).first()
    if incident is None:
        raise RuntimeError("incident not found")
    if payload.get("followup"):
        return add_resolved_followup(db, incident)
    return create_glpi_ticket(db, incident, actor)


# ---- Outbound webhook -----------------------------------------------------------------------

def webhook_ready() -> str:
    cfg = _cfg("webhook")
    if not cfg.get("enabled"):
        return "the ticket webhook is off (tickets.webhook.enabled)"
    if not str(cfg.get("url") or "").startswith(("https://", "http://")):
        return "no URL (tickets.webhook.url)"
    return ""


def webhook_payload(incident: Incident, event: str) -> dict[str, Any]:
    asset = incident.asset
    return {"event": event, "source": "forgesre", "at": utcnow().isoformat(),
            "incident": {"number": incident.number.split("_")[0], "id": incident.number, "title": incident.title,
                         "severity": incident.severity, "status": incident.status,
                         "host": asset.hostname if asset is not None else "", "ip": asset.ip if asset is not None else "",
                         "started_at": incident.started_at.isoformat() if incident.started_at else None,
                         "acknowledged_by": incident.ack_by or "", "resolved_by": incident.resolved_by or "",
                         "root_cause": incident.root_cause or "", "url": _link(incident),
                         "ticket": incident.ticket_ref or ""}}


def queue_webhook(db: Session, incident: Incident, event: str, key: str) -> int:
    from app.jobs import enqueue

    if webhook_ready():
        return 0
    step_key = f"webhook:{key}"[:64]
    if db.query(Notification).filter_by(incident_id=incident.id, step_key=step_key).first():
        return 0
    row = Notification(incident_id=incident.id, channel="webhook", target=str(_cfg("webhook").get("url"))[:255],
                       subject=incident.title[:255], body=event, status="queued", step_key=step_key)
    db.add(row)
    db.commit()
    enqueue(db, WEBHOOK_KIND, str(row.id), object_type="notification")
    return 1


def queue_step(db: Session, incident: Incident, step_key: str, after_minutes: int | None = None) -> int:
    try:
        first = after_minutes == 0 if after_minutes is not None else step_key == "immediate"
        return queue_webhook(db, incident, "opened" if first else "escalated", step_key)
    except Exception:  # the webhook must never break escalation mail
        log.exception("ticket webhook queue failed for %s", incident.number)
        db.rollback()
        return 0


def deliver_webhook(db: Session, notification_id: int) -> bool:
    from app.journal import report

    row = db.get(Notification, notification_id)
    if row is None or row.channel != "webhook":
        raise RuntimeError(f"webhook notification {notification_id} not found")
    incident = db.get(Incident, row.incident_id) if row.incident_id else None
    why = webhook_ready()
    ok, detail = False, why
    if not why and incident is not None:
        token = settings._secret("TICKET_WEBHOOK_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = httpx.post(str(_cfg("webhook").get("url")), json=webhook_payload(incident, row.body), headers=headers, timeout=TIMEOUT)
            ok, detail = 200 <= response.status_code < 300, f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}: could not reach the ticket webhook"
    row.status, row.error = ("sent", "") if ok else ("failed", detail)
    db.commit()
    report(db, "notification", "ticket.webhook", "ok" if ok else "error", summary=f"Ticket webhook {row.body}: {row.subject}", detail=detail)
    return ok


def sweep_resolved(db: Session, window_minutes: int = 60) -> int:
    """Resolved events: the webhook, and the GLPI follow-up, for incidents resolved lately."""
    from datetime import timedelta

    since = utcnow() - timedelta(minutes=window_minutes)
    rows = db.query(Incident).filter(Incident.resolved_at >= since).limit(200).all()
    sent = 0
    for inc in rows:
        has_hook = db.query(Notification).filter(Notification.incident_id == inc.id, Notification.step_key.like("webhook:%")).first()
        if has_hook is not None:
            sent += queue_webhook(db, inc, "resolved", "resolved")
        if inc.ticket_ref and not db.query(Notification).filter_by(incident_id=inc.id, step_key="ticket:resolved").first():
            db.add(Notification(incident_id=inc.id, channel="ticket", target=inc.ticket_ref, subject=inc.title[:255],
                                body="resolved follow-up", status="sent", step_key="ticket:resolved"))
            db.commit()
            note_resolved(db, inc)
    return sent


def check_glpi() -> dict[str, Any]:
    why = glpi_ready()
    if why:
        return {"status": "disabled" if "are off" in why else "error", "why": why}
    from app.glpi import init_session, kill_session

    try:
        session = init_session(settings.glpi_url, settings.glpi_app_token, settings.glpi_user_token, timeout=settings.glpi_timeout, force=True)
        kill_session(settings.glpi_url, settings.glpi_app_token, session, timeout=settings.glpi_timeout)
    except Exception as exc:  # noqa: BLE001 — a check reports
        return {"status": "error", "why": f"GLPI sign-in failed: {str(exc)[:160]}"}
    return {"status": "ok", "why": "GLPI answers; tickets will be created with the integration's user"}


def check_webhook() -> dict[str, Any]:
    why = webhook_ready()
    if why:
        return {"status": "disabled" if "is off" in why else "error", "why": why}
    token = settings._secret("TICKET_WEBHOOK_TOKEN")
    try:
        response = httpx.post(str(_cfg("webhook").get("url")), json={"event": "test", "source": "forgesre", "at": utcnow().isoformat()},
                              headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return {"status": "error", "why": f"{type(exc).__name__}: could not reach the webhook"}
    return {"status": "ok" if response.status_code < 300 else "error", "why": f"test event: HTTP {response.status_code}"}
