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


COLORS = {"CRITICAL": "#dc2626", "HIGH": "#ea580c", "WARNING": "#ca8a04", "resolved": "#16a34a"}


def _incident_link(incident: Incident) -> str:
    base = (settings.yaml.get("system") or {}).get("public_url") or ""
    return f"{str(base).rstrip('/')}/app/incidents/{incident.number}" if base else ""


def _facts(incident: Incident) -> list[tuple[str, str]]:
    asset = incident.asset
    started = incident.started_at.strftime("%Y-%m-%d %H:%M UTC") if incident.started_at else ""
    rows = [("Host", asset.hostname if asset is not None else ""), ("Incident", incident.number),
            ("Severity", (incident.severity or "").title()), ("Started", started)]
    return [(k, v) for k, v in rows if v]


def payload(channel: str, text: str, incident: Incident | None = None, event: str = "") -> dict[str, Any]:
    """Body each service expects from an incoming webhook.

    With the incident, a card in the severity colour (green when resolved) with host, number,
    severity, start time and an "Open incident" link when system.public_url is set; ``text``
    stays as the notification fallback. Only those fields leave the server.
    """
    if incident is None:
        if channel == "slack":
            return {"text": text}
        if channel == "discord":
            return {"content": text[:1900], "allowed_mentions": {"parse": []}}
        return _teams(text, [], "", "default")
    first = text.splitlines()[0]
    head = first.split(" · ", 1)[0] + (" — " + first.split(" — ", 1)[1] if " — " in first else "")
    link = _incident_link(incident)
    facts = _facts(incident)
    color = COLORS["resolved"] if event == "resolved" else COLORS.get((incident.severity or "").upper(), COLORS["WARNING"])
    if channel == "slack":
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{head}*"}},
            {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in facts]},
        ]
        if link:
            blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open incident"}, "url": link}]})
        return {"text": text, "attachments": [{"color": color, "blocks": blocks}]}
    if channel == "discord":
        embed: dict[str, Any] = {"title": head[:256], "color": int(color[1:], 16),
                                 "fields": [{"name": k, "value": v[:1024], "inline": True} for k, v in facts]}
        if link:
            embed["url"] = link
        return {"content": "", "embeds": [embed], "allowed_mentions": {"parse": []}}
    tone = "good" if event == "resolved" else ("attention" if (incident.severity or "").upper() == "CRITICAL" else "warning")
    return _teams(head, facts, link, tone)


def _teams(head: str, facts: list[tuple[str, str]], link: str, tone: str) -> dict[str, Any]:
    # Teams Workflows ("When a Teams webhook request is received") takes an Adaptive Card.
    body: list[dict[str, Any]] = [{"type": "TextBlock", "text": head, "wrap": True, "weight": "Bolder", "color": tone}]
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]})
    card: dict[str, Any] = {"type": "AdaptiveCard", "version": "1.4",
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "body": body}
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open incident", "url": link}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def _event_of(text: str) -> str:
    head = text.split("] ", 1)[-1].split(":", 1)[0].strip().lower()
    return head if head in {"opened", "escalated", "resolved"} else ""


def post(channel: str, text: str, timeout: float = TIMEOUT, incident: Incident | None = None) -> tuple[bool, str]:
    """POST one message. Never raises: returns (ok, detail). The URL is never logged (it is a secret);
    the Notifications flow page gets the host, result and time only."""
    import time
    from urllib.parse import urlsplit

    from app.exchanges import record

    started = time.monotonic()
    ok, detail = _post(channel, text, timeout, incident)
    record("notifications", "out", f"chat.{channel}", peer=urlsplit(webhook_url(channel)).hostname or "", status="ok" if ok else "error",
           ms=int((time.monotonic() - started) * 1000), summary=f"{LABELS[channel]}: {detail}" + (f" · {incident.number.split('_')[0]}" if incident else ""))
    return ok, detail


def _post(channel: str, text: str, timeout: float, incident: Incident | None) -> tuple[bool, str]:
    url = webhook_url(channel)
    if not url:
        return False, f"{CHANNELS[channel]} is not set"
    if not url.lower().startswith("https://"):
        return False, f"{CHANNELS[channel]} must be an https:// URL"
    try:
        response = httpx.post(url, json=payload(channel, text, incident=incident, event=_event_of(text)), timeout=timeout)
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
    """Queue a 'resolved' message for recently resolved incidents that had a chat message.

    The incidents are picked by id (IN subquery), not with SELECT DISTINCT over the incident row:
    Postgres cannot compare its JSON columns, and that query failed every 30 s.
    """
    since = utcnow() - timedelta(minutes=window_minutes)
    chatted = db.query(Notification.incident_id).filter(Notification.step_key.like("chat:%"))
    rows = db.query(Incident).filter(Incident.resolved_at >= since, Incident.id.in_(chatted)).all()
    return sum(_queue(db, row, "resolved", "resolved") for row in rows)


def deliver(db: Session, notification_id: int) -> bool:
    """Jobs worker: post one queued chat message and record the result on the Notification row."""
    from app.journal import report

    row = db.get(Notification, notification_id)
    if row is None or row.channel not in CHANNELS:
        raise RuntimeError(f"chat notification {notification_id} not found")
    incident = db.get(Incident, row.incident_id) if row.incident_id else None
    ok, detail = post(row.channel, row.body, incident=incident)
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
        return {"status": "disabled", "why": "no chat channel turned on"}
    missing = [c for c in on if not webhook_url(c)]
    if missing:
        return {"status": "error", "why": "on but no webhook URL: " + ", ".join(CHANNELS[c] for c in missing)}
    failed = []
    if db is not None:
        for c in on:
            last = db.query(Notification).filter_by(channel=c).order_by(Notification.id.desc()).first()
            if last is not None and last.status == "failed":
                failed.append(f"{LABELS[c]}: {last.error}")
    if failed:
        return {"status": "error", "why": "; ".join(failed)[:300]}
    return {"status": "ok", "why": ", ".join(LABELS[c] for c in on)}
