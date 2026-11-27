"""SMS paging to the person on call, through an SMS gateway (optional, off by default).

Who: the on-call slot that covers now (its phone). When: an incident at or above
``notifications.sms.min_severity`` opens, and each time it escalates. How: one HTTP call per
message from a background job (10 s timeout), recorded as a Notification with channel ``sms``.

Providers:
- ``twilio``: the Twilio Messages API; ``notifications.sms.account`` = Account SID,
  ``notifications.sms.from`` = the sending number, secret ``SMS_GATEWAY_TOKEN`` = Auth Token.
- ``generic``: any gateway that takes a POST; ``notifications.sms.url`` and a JSON body template
  with ``{to}`` and ``{text}``; secret ``SMS_GATEWAY_TOKEN`` is sent as ``Authorization: Bearer``.
  Many gateways (and voice-call services) accept this form.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import Incident, Notification
from app.settings import settings

log = logging.getLogger("forgesre")

JOB_KIND = "sms_send"
TIMEOUT = 10.0
RANK = {"warning": 1, "high": 2, "critical": 3}
TEXT_MAX = 300
DEFAULT_BODY = '{"to": "{to}", "text": "{text}"}'


def config() -> dict[str, Any]:
    raw = ((settings.yaml.get("notifications") or {}).get("sms") or {}) if isinstance(settings.yaml, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "provider": str(raw.get("provider") or "generic"),
        "url": str(raw.get("url") or ""),
        "account": str(raw.get("account") or ""),
        "from": str(raw.get("from") or ""),
        "body": str(raw.get("body") or DEFAULT_BODY),
        "min_severity": str(raw.get("min_severity") or "critical").lower(),
    }


def not_ready(cfg: dict[str, Any] | None = None) -> str:
    """Why SMS cannot be sent, or "" when it can."""
    cfg = cfg or config()
    if not cfg["enabled"]:
        return "SMS is off (notifications.sms.enabled)"
    if not settings._secret("SMS_GATEWAY_TOKEN"):
        return "no SMS_GATEWAY_TOKEN (./forgesre settings set-secret SMS_GATEWAY_TOKEN)"
    if cfg["provider"] == "twilio" and not (cfg["account"] and cfg["from"]):
        return "Twilio needs notifications.sms.account (Account SID) and notifications.sms.from"
    if cfg["provider"] != "twilio" and not cfg["url"].startswith(("https://", "http://")):
        return "no gateway URL (notifications.sms.url)"
    return ""


def wants(incident: Incident) -> bool:
    cfg = config()
    return not not_ready(cfg) and RANK.get((incident.severity or "").lower(), 0) >= RANK.get(cfg["min_severity"], 3)


def message(incident: Incident, event: str) -> str:
    host = incident.asset.hostname if incident.asset is not None else ""
    head = {"opened": "NEW", "escalated": "ESCALATED"}.get(event, event.upper())
    text = f"ForgeSRE {head} [{(incident.severity or '').upper()}] {incident.title}"
    if host:
        text += f" on {host}"
    text += f" ({incident.number.split('_')[0]})"
    base = (settings.yaml.get("system") or {}).get("public_url") or ""
    if base:
        text += f" {str(base).rstrip('/')}/app/incidents/{incident.number}"
    return text[:TEXT_MAX]


def send(to: str, text: str) -> tuple[bool, str]:
    """One SMS now. Returns (ok, detail); never raises and never logs the token. Logged for the
    Notifications flow page with the number masked."""
    import time

    from app.exchanges import mask_address, record

    started = time.monotonic()
    ok, detail = _send(to, text)
    record("notifications", "out", "sms", peer=config().get("provider", ""), status="ok" if ok else "error",
           ms=int((time.monotonic() - started) * 1000), summary=f"to {mask_address(to)}: {detail}")
    return ok, detail


def _send(to: str, text: str) -> tuple[bool, str]:
    cfg = config()
    why = not_ready(cfg)
    if why:
        return False, why
    token = settings._secret("SMS_GATEWAY_TOKEN")
    try:
        if cfg["provider"] == "twilio":
            url = f"https://api.twilio.com/2010-04-01/Accounts/{cfg['account']}/Messages.json"
            response = httpx.post(url, data={"To": to, "From": cfg["from"], "Body": text}, auth=(cfg["account"], token), timeout=TIMEOUT)
        else:
            body = cfg["body"].replace("{to}", json.dumps(to)[1:-1]).replace("{text}", json.dumps(text)[1:-1])
            try:
                payload: Any = json.loads(body)
            except json.JSONDecodeError:
                return False, "notifications.sms.body is not valid JSON after filling {to} and {text}"
            response = httpx.post(cfg["url"], json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: could not reach the SMS gateway"
    if 200 <= response.status_code < 300:
        return True, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code} from the SMS gateway"


def queue(db: Session, incident: Incident, event: str, key: str) -> int:
    """Queue an SMS to the person on call for this incident event (once per key)."""
    from app.dc_noc import current_oncall
    from app.jobs import enqueue

    if not wants(incident):
        return 0
    person = current_oncall(db) or {}
    phone = str(person.get("phone") or "").strip()
    if not phone:
        return 0
    step_key = f"sms:{key}"[:64]
    if db.query(Notification).filter_by(incident_id=incident.id, step_key=step_key).first():
        return 0
    row = Notification(incident_id=incident.id, channel="sms", target=phone, subject=incident.title[:255],
                       body=message(incident, event), status="queued", step_key=step_key)
    db.add(row)
    db.commit()
    enqueue(db, JOB_KIND, str(row.id), object_type="notification")
    return 1


def queue_step(db: Session, incident: Incident, step_key: str, after_minutes: int | None = None) -> int:
    """Called after an escalation step: the first step is 'opened', later ones 'escalated'."""
    try:
        first = after_minutes == 0 if after_minutes is not None else step_key == "immediate"
        return queue(db, incident, "opened" if first else "escalated", step_key)
    except Exception:  # SMS must never break escalation mail
        log.exception("sms queue failed for %s", incident.number)
        db.rollback()
        return 0


def deliver(db: Session, notification_id: int) -> bool:
    """Jobs worker: send one queued SMS and record the result on the Notification row."""
    from app.journal import report

    row = db.get(Notification, notification_id)
    if row is None or row.channel != "sms":
        raise RuntimeError(f"sms notification {notification_id} not found")
    ok, detail = send(row.target, row.body)
    row.status = "sent" if ok else "failed"
    row.error = "" if ok else detail
    db.commit()
    report(db, "notification", "sms", "ok" if ok else "error", summary=f"SMS to {row.target}: {row.subject} ({row.status})", detail=detail)
    return ok


def check(db: Session | None = None) -> dict[str, Any]:
    """Settings › Test: one test SMS to the person on call now."""
    from app.db import SessionLocal
    from app.dc_noc import current_oncall

    why = not_ready()
    if why:
        return {"status": "disabled" if "is off" in why else "error", "why": why}
    own = db is None
    db = db or SessionLocal()
    try:
        phone = str((current_oncall(db) or {}).get("phone") or "").strip()
    finally:
        if own:
            db.close()
    if not phone:
        return {"status": "error", "why": "nobody is on call now, or the on-call slot has no phone", "fix": "Incidents › On-call"}
    ok, detail = send(phone, "ForgeSRE test SMS. Paging works.")
    return {"status": "ok" if ok else "error", "why": f"test SMS to {phone}: {detail}"}
