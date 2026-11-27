"""Notifications flow: from an incident to a person, block by block (registered in app.flows).

Lane "Incident → person": escalation steps of the last 24 h, who is on call now, which channels are
on. Then one lane per channel that is on: e-mail (DNS → TCP → sign-in with STARTTLS, no mail is sent
by the check → what was delivered), chat (Slack / Teams / Discord: DNS → TCP → TLS to the webhook
host → what was posted; the webhook URL is a secret and never shown), web push (subscriptions →
results) and SMS. Read-only: the check never sends a message.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.flows import Flow, network_steps, register, step
from app.models import Notification, utcnow
from app.settings import settings

DELIVERED = {"sent"}
FAILED = {"failed"}


def _rows(db: Session, channels: list[str], limit: int = 15) -> list[dict[str, Any]]:
    """The last notifications of these channels as detail rows (recipient masked)."""
    from app.exchanges import mask_address

    out = []
    for n in (db.query(Notification).filter(Notification.channel.in_(channels)).order_by(Notification.id.desc()).limit(limit).all()):
        out.append({"at": n.created_at.isoformat() if n.created_at else "", "status": n.status, "error": (n.error or "")[:200],
                    "subject": f"{mask_address(n.target) if n.channel in {'email', 'sms'} else n.channel} · {(n.subject or '')[:80]}"})
    return out


def _delivered_step(db: Session, sid: str, label: str, channels: list[str], kind: str) -> dict[str, Any]:
    rows = _rows(db, channels)
    if not rows:
        return step(sid, label, "last messages", "unknown", "Nothing sent on this channel yet.", log={"direction": "out", "kinds": [kind]})
    last = rows[0]
    failed = [r for r in rows if r["status"] in FAILED]
    status = "bad" if last["status"] in FAILED else ("warn" if failed or last["status"] not in DELIVERED else "ok")
    text = f"Last: {last['status']} at {last['at'][11:16]} UTC" + (f" — {last['error']}" if last["error"] else "") + f"; {len(failed)} of the last {len(rows)} failed."
    return step(sid, label, f"{len(rows)} recent", status, text, rows=rows, log={"direction": "out", "kinds": [kind]},
                fix="" if status == "ok" else "Open the failed row's error; the blocks before this one show where the path breaks.")


def _pipeline(db: Session, on: dict[str, bool]) -> list[dict[str, Any]]:
    from app.dc_noc import current_oncall
    from app.services import NO_RECIPIENT_STATUS

    since = utcnow() - timedelta(days=1)
    recent = db.query(Notification).filter(Notification.created_at >= since, Notification.incident_id.isnot(None)).all()
    missing = [n for n in recent if n.status == NO_RECIPIENT_STATUS]
    rows = [{"at": n.created_at.isoformat(), "status": n.status, "error": n.error or "", "subject": f"step {n.step_key} · role {n.target}"} for n in missing[:15]]
    steps = [step("escalation", "Escalation", f"{len(recent)} in 24 h", "bad" if missing else ("ok" if recent else "unknown"),
                  (f"{len(missing)} escalation step(s) had nobody to send to." if missing else
                   f"{len(recent)} notification(s) created in the last 24 h." if recent else "No incident was escalated in the last 24 h."),
                  rows=rows, fix="Give the asset an owner e-mail, or put someone on call (Incidents › On-call), or an address on the escalation step." if missing else "")]
    person = current_oncall(db, utcnow())
    steps.append(step("oncall", "On call", (person or {}).get("name") or "nobody", "ok" if person and person.get("email") else "warn",
                      f"{person.get('name')} ({person.get('email') or 'no e-mail'}) is on call now." if person else "Nobody is on the on-call roster right now.",
                      fix="" if person and person.get("email") else "Incidents › On-call: add the shift, with an e-mail address."))
    names = [label for key, label in (("email", "e-mail"), ("chat", "chat"), ("push", "web push"), ("sms", "SMS")) if on[key]]
    steps.append(step("channels", "Channels", f"{len(names)} on", "ok" if names else "bad",
                      f"On: {', '.join(names)}." if names else "No notification channel is on: nobody is told about an incident.",
                      fix="" if names else "Settings › Notifications: turn on e-mail or a chat channel."))
    return steps


def _email_lane(db: Session) -> list[dict[str, Any]]:
    from app.email_service import check as smtp_check

    host, port = settings.smtp_host, int(settings.smtp_port or 587)
    steps = [step("smtp_cfg", "Settings", f"{host}:{port}" if host else "no server", "ok" if host else "bad",
                  f"SMTP {host}:{port}, {'STARTTLS' if settings.smtp_tls else 'no TLS'}, from {settings.smtp_from or '?'}." if host else "No SMTP server is set.",
                  fix="" if host else "Settings › Notifications › SMTP server.")]
    if not host:
        return steps
    net, reachable = network_steps(f"smtp://{host}:{port}", what="the SMTP server", tls=False, prefix="smtp_")
    steps += net
    if not reachable:
        steps.append(_delivered_step(db, "mail_sent", "Delivered", ["email"], "email"))
        return steps
    result = smtp_check(send=False)
    steps.append(step("smtp_login", "Sign-in", "STARTTLS + login" if settings.smtp_tls else "login",
                      "ok" if result.get("status") == "ok" else "bad", str(result.get("why") or ""), fix=str(result.get("fix") or "")))
    steps.append(_delivered_step(db, "mail_sent", "Delivered", ["email"], "email"))
    return steps


def _chat_lane(db: Session) -> list[dict[str, Any]]:
    from app.chat import CHANNELS, LABELS, channel_config, webhook_url

    steps: list[dict[str, Any]] = []
    for channel in CHANNELS:
        if not channel_config(channel)["enabled"]:
            continue
        url = webhook_url(channel)
        label = LABELS[channel]
        if not url:
            steps.append(step(f"{channel}_cfg", label, "webhook", "bad", f"{label} is on but its webhook URL is empty.",
                              fix=f"./forgesre settings set-secret {CHANNELS[channel]}"))
            continue
        parts = urlsplit(url)
        host = parts.hostname or ""
        steps.append(step(f"{channel}_cfg", label, host, "ok" if url.lower().startswith("https://") else "bad",
                          f"Posts to {host} (the full URL is a secret).", fix="" if url.lower().startswith("https://") else "The webhook URL must start with https://."))
        net, _ = network_steps(f"https://{host}:{parts.port or 443}", what=label, prefix=f"{channel}_")
        steps += net
        steps.append(_delivered_step(db, f"{channel}_sent", "Posted", [channel], f"chat.{channel}"))
    return steps


def _push_lane(db: Session) -> list[dict[str, Any]]:
    from app.models import PushSubscription

    subs = db.query(PushSubscription).all()
    failing = [s for s in subs if (s.fails or 0) > 0]
    rows = [{"at": s.last_ok_at.isoformat() if s.last_ok_at else "", "status": "failing" if (s.fails or 0) else "ok",
             "error": (getattr(s, "last_error", "") or "")[:120], "subject": f"{s.email.split('@')[0][:1]}… · {urlsplit(s.endpoint).hostname}"} for s in subs[:15]]
    return [step("push_subs", "Subscriptions", f"{len(subs)} device(s)", "ok" if subs and not failing else ("warn" if subs else "unknown"),
                 (f"{len(subs)} phone(s) or browser(s) subscribed; {len(failing)} failing." if subs else "Nobody has turned on push yet (Settings › Notifications › Web push on the phone)."),
                 rows=rows, log={"direction": "out", "kinds": ["web push"]},
                 fix="A failing device: turn push off and on again on it." if failing else "")]


def _sms_lane(db: Session) -> list[dict[str, Any]]:
    from app.sms import config, not_ready

    why = not_ready(config())
    return [step("sms_cfg", "Settings", config().get("provider", ""), "bad" if why else "ok", why or "SMS gateway is set.",
                 fix="Settings › Notifications › SMS." if why else ""),
            _delivered_step(db, "sms_sent", "Delivered", ["sms"], "sms")]


def _on() -> dict[str, bool]:
    from app import web_push
    from app.chat import CHANNELS, channel_config
    from app.sms import config as sms_config

    return {"email": bool(settings.email_enabled), "chat": any(channel_config(c)["enabled"] for c in CHANNELS),
            "push": web_push.enabled(), "sms": bool(sms_config().get("enabled"))}


def check(db: Session) -> dict[str, Any]:
    on = _on()
    lanes = [{"id": "in", "title": "Incident → person (escalation)", "steps": _pipeline(db, on)}]
    if on["email"]:
        lanes.append({"id": "email", "title": "E-mail (SMTP)", "steps": _email_lane(db)})
    if on["chat"]:
        lanes.append({"id": "chat", "title": "Chat (Slack, Teams, Discord)", "steps": _chat_lane(db)})
    if on["push"]:
        lanes.append({"id": "push", "title": "Web push (phones and browsers)", "steps": _push_lane(db)})
    if on["sms"]:
        lanes.append({"id": "sms", "title": "SMS", "steps": _sms_lane(db)})
    return {"lanes": lanes}


FLOW = register(Flow(
    name="notifications", label="Notifications", check=check, configured=lambda: any(_on().values()),
    log_words=("smtp", "notification", "chat", "push", "sms", "escalat"),
    about="How an incident reaches a person: escalation, on call, and each channel (e-mail, chat, web push, SMS). The check never sends a message.",
))
