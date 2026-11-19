"""Chat notifications: Slack, Microsoft Teams and Discord incoming webhooks.

Off until an Admin sets a channel's webhook URL (secrets.env) and turns it on in Settings.
Chat is an extra channel next to email: a message goes out when an incident opens, at each
escalation step and when it resolves, for incidents at or above the channel's minimum
severity. Messages leave the server, so they carry only the incident line, never logs,
evidence or ForgeAI text.

Nothing here posts inside a request or the alert path: ``queue_*`` writes a Notification row
and a ``chat_post`` job; the jobs worker calls ``deliver`` with a 10 s timeout.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import Incident, Notification, utcnow
from app.settings import settings

log = logging.getLogger("forgesre")

CHANNELS: dict[str, str] = {"slack": "SLACK_WEBHOOK_URL", "teams": "TEAMS_WEBHOOK_URL", "discord": "DISCORD_WEBHOOK_URL"}
LABELS = {"slack": "Slack", "teams": "Microsoft Teams", "discord": "Discord"}
SEVERITY_RANK = {"WARNING": 1, "HIGH": 2, "CRITICAL": 3}
JOB_KIND = "chat_post"
TIMEOUT = 10.0


def webhook_url(channel: str) -> str:
    return os.environ.get(CHANNELS[channel], "").strip()


def channel_config(channel: str) -> dict[str, Any]:
    """Enabled flag and minimum severity from config/forgesre.yml (notifications.chat.<channel>)."""
    block = ((settings.yaml.get("notifications") or {}).get("chat") or {}).get(channel) or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "min_severity": str(block.get("min_severity") or "high").upper(),
    }


def active_channels(severity: str | None = None) -> list[str]:
    """Channels that are on, have a URL, and (when given) accept this severity."""
    rank = SEVERITY_RANK.get(str(severity or "").upper(), 1)
    out = []
    for channel in CHANNELS:
        cfg = channel_config(channel)
        if cfg["enabled"] and webhook_url(channel) and rank >= SEVERITY_RANK.get(cfg["min_severity"], 2):
            out.append(channel)
    return out


def message_text(incident: Incident, event: str, detail: str = "") -> str:
    """One short line per event. Only fields an on-call person needs; no logs or evidence."""
    host = incident.asset.hostname if incident.asset is not None else ""
    head = {"opened": "Opened", "escalated": "Escalated", "resolved": "Resolved"}.get(event, event.title())
    parts = [f"[{(incident.severity or '').upper()}] {head}: {incident.title}", host, incident.number]
    text = " · ".join(p for p in parts if p)
    if detail:
        text += f" — {detail}"
    base = (settings.yaml.get("system") or {}).get("public_url") or ""
    if base:
        text += f"\n{str(base).rstrip('/')}/app/incidents/{incident.number}"
    return text


def payload(channel: str, text: str) -> dict[str, Any]:
    """Body each service expects from an incoming webhook."""
    if channel == "slack":
        return {"text": text}
    if channel == "discord":
        return {"content": text[:1900], "allowed_mentions": {"parse": []}}
    # Teams Workflows ("When a Teams webhook request is received") takes an Adaptive Card.
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }
        ],
    }


def post(channel: str, text: str, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """POST one message. Never raises: returns (ok, detail). The URL is never logged (it is a secret)."""
    url = webhook_url(channel)
    if not url:
        return False, f"{CHANNELS[channel]} is not set"
    if not url.lower().startswith("https://"):
        return False, f"{CHANNELS[channel]} must be an https:// URL"
    try:
        response = httpx.post(url, json=payload(channel, text), timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: could not reach {LABELS[channel]}"
    if 200 <= response.status_code < 300:
        return True, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code} from {LABELS[channel]}"


def _queue(db: Session, incident: Incident, event: str, key: str, detail: str = "") -> int:
    from app.jobs import enqueue

    queued = 0
    for channel in active_channels(incident.severity):
        step_key = f"chat:{channel}:{key}"[:64]
        if db.query(Notification).filter_by(incident_id=incident.id, step_key=step_key).first():
            continue
        row = Notification(
            incident_id=incident.id, channel=channel, target=LABELS[channel], subject=incident.title[:255],
            body=message_text(incident, event, detail), status="queued", step_key=step_key,
        )
        db.add(row)
        db.commit()
        enqueue(db, JOB_KIND, str(row.id), object_type="notification")
        queued += 1
    return queued


def queue_step(db: Session, incident: Incident, step_key: str, target: str, after_minutes: int | None = None) -> int:
    """Called after an escalation step was created. A 0-minute step counts as 'opened'."""
    try:
        first = after_minutes == 0 if after_minutes is not None else step_key == "immediate"
        event = "opened" if first else "escalated"
        return _queue(db, incident, event, step_key, "" if event == "opened" else f"to {target}")
    except Exception:  # chat must never break escalation mail
        log.exception("chat queue failed for %s", incident.number)
        db.rollback()
        return 0


def sweep_resolved(db: Session, window_minutes: int = 60) -> int:
    """Queue a 'resolved' message for recently resolved incidents that had a chat message."""
    since = utcnow() - timedelta(minutes=window_minutes)
    rows = (
        db.query(Incident)
        .join(Notification, Notification.incident_id == Incident.id)
        .filter(Incident.resolved_at >= since, Notification.step_key.like("chat:%"))
        .distinct()
        .all()
    )
    return sum(_queue(db, row, "resolved", "resolved") for row in rows)


def deliver(db: Session, notification_id: int) -> bool:
    """Jobs worker: post one queued chat message and record the result on the Notification row."""
    from app.journal import report

    row = db.get(Notification, notification_id)
    if row is None or row.channel not in CHANNELS:
        raise RuntimeError(f"chat notification {notification_id} not found")
    ok, detail = post(row.channel, row.body)
    row.status = "sent" if ok else "failed"
    row.error = "" if ok else detail
    db.commit()
    report(db, "notification", f"chat.{row.channel}", "ok" if ok else "error", summary=f"{LABELS[row.channel]}: {row.subject} ({row.status})", detail=detail)
    return ok


def send_test(channel: str) -> dict[str, Any]:
    """Settings › Test: post one line now (an Admin clicked it, so this one is synchronous)."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown chat channel {channel}")
    ok, detail = post(channel, "ForgeSRE test message: this channel will receive incident updates.")
    return {"status": "ok" if ok else "error", "detail": detail}


def doctor_check(db: Session | None = None) -> dict[str, Any]:
    """Doctor row: which channels are on, and whether the last message to each was delivered."""
    on = [c for c in CHANNELS if channel_config(c)["enabled"]]
    if not on:
        return {"status": "disabled", "detail": "no chat channel turned on"}
    missing = [c for c in on if not webhook_url(c)]
    if missing:
        return {"status": "error", "detail": "on but no webhook URL: " + ", ".join(CHANNELS[c] for c in missing)}
    failed = []
    if db is not None:
        for c in on:
            last = db.query(Notification).filter_by(channel=c).order_by(Notification.id.desc()).first()
            if last is not None and last.status == "failed":
                failed.append(f"{LABELS[c]}: {last.error}")
    if failed:
        return {"status": "error", "detail": "; ".join(failed)[:300]}
    return {"status": "ok", "detail": ", ".join(LABELS[c] for c in on)}
