"""Slack / Teams / Discord notifications: config, queueing, delivery, doctor. No network."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app import chat
from app.db import Base, SessionLocal, engine
from app.models import Asset, Incident, Job, Notification, utcnow
from app.settings import settings


@pytest.fixture()
def chat_on(monkeypatch):
    """Slack on for high and up, Discord on for critical only, Teams off."""
    data = dict(settings.yaml or {})
    data["notifications"] = {**(data.get("notifications") or {}), "chat": {
        "slack": {"enabled": True, "min_severity": "high"},
        "teams": {"enabled": False},
        "discord": {"enabled": True, "min_severity": "critical"},
    }}
    old = settings.yaml
    settings.yaml = data
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T/B/x")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/api/webhooks/1/x")
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    yield
    settings.yaml = old


def _incident(db, severity="HIGH") -> Incident:
    tag = uuid4().hex[:6]
    asset = Asset(asset_id=f"chat-{tag}", hostname=f"chat-{tag}", ip="10.55.0.1", type="Linux Server")
    db.add(asset)
    db.flush()
    row = Incident(number=f"INC-C{tag}", title="Disk /u02 92%", severity=severity, status="OPEN",
                   fingerprint=f"chat:{tag}", asset_id=asset.id, started_at=utcnow())
    db.add(row)
    db.commit()
    return row


def test_rich_payloads_carry_only_the_incident_line():
    from app.models import Asset, Incident

    incident = Incident(number="INC-9", title="Host down", severity="CRITICAL", status="OPEN", source="prometheus",
                        asset=Asset(asset_id="a-9", hostname="esx-9", ip="10.0.0.9", type="Hypervisor"))
    text = chat.message_text(incident, "resolved")
    discord = chat.payload("discord", text, incident=incident, event="resolved")["embeds"][0]
    assert discord["color"] == int(chat.COLORS["resolved"][1:], 16) and discord["title"] == "[CRITICAL] Resolved: Host down"
    teams = chat.payload("teams", text, incident=incident, event="opened")["attachments"][0]["content"]
    assert teams["body"][0]["color"] == "attention"
    assert {"title": "Host", "value": "esx-9"} in teams["body"][1]["facts"]
    assert "10.0.0.9" not in str(teams)  # no IP, logs or evidence leave the server


def test_payload_shapes():
    assert chat.payload("slack", "hi") == {"text": "hi"}
    discord = chat.payload("discord", "x" * 3000)
    assert len(discord["content"]) == 1900 and discord["allowed_mentions"] == {"parse": []}
    card = chat.payload("teams", "hi")["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["body"][0]["text"] == "hi"


def test_channels_follow_severity_and_need_a_url(chat_on):
    assert chat.active_channels("WARNING") == []
    assert chat.active_channels("HIGH") == ["slack"]
    assert chat.active_channels("CRITICAL") == ["slack", "discord"]


def test_disabled_by_default(monkeypatch):
    old = settings.yaml
    settings.yaml = {k: v for k, v in (old or {}).items() if k != "notifications"}
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T/B/x")
    try:
        assert chat.active_channels("CRITICAL") == []
        assert chat.doctor_check()["status"] == "disabled"
    finally:
        settings.yaml = old


def test_step_queues_a_job_and_delivery_records_the_result(chat_on, monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    incident = _incident(db, "HIGH")
    assert chat.queue_step(db, incident, "immediate", "team@dc.local", 0) == 1
    assert chat.queue_step(db, incident, "immediate", "team@dc.local", 0) == 0  # once per step
    row = db.query(Notification).filter_by(incident_id=incident.id, channel="slack").one()
    assert row.status == "queued" and row.step_key == "chat:slack:immediate"
    assert row.body.startswith("[HIGH] Opened: Disk /u02 92%") and incident.number in row.body
    job = db.query(Job).filter_by(kind=chat.JOB_KIND, object_id=str(row.id)).one()
    assert job.status == "pending"

    sent = []

    def fake_post(url, json, timeout):
        sent.append((url, json, timeout))
        return httpx.Response(200)

    monkeypatch.setattr(chat.httpx, "post", fake_post)
    assert chat.deliver(db, row.id) is True
    db.refresh(row)
    body = sent[0][1]
    assert row.status == "sent" and body["text"] == row.body and sent[0][2] == chat.TIMEOUT
    card = body["attachments"][0]
    assert card["color"] == chat.COLORS["HIGH"] and card["blocks"][0]["text"]["text"] == "*[HIGH] Opened: Disk /u02 92%*"
    assert {"type": "mrkdwn", "text": f"*Incident*\n{incident.number}"} in card["blocks"][1]["fields"]

    chat.queue_step(db, incident, "15m", "lead@dc.local", 15)
    later = db.query(Notification).filter_by(incident_id=incident.id, step_key="chat:slack:15m").one()
    assert "Escalated" in later.body and "to lead@dc.local" in later.body
    monkeypatch.setattr(chat.httpx, "post", lambda url, json, timeout: httpx.Response(404))
    assert chat.deliver(db, later.id) is False
    db.refresh(later)
    assert later.status == "failed" and "HTTP 404" in later.error
    assert chat.doctor_check(db)["status"] == "error"
    db.close()


def test_network_error_never_raises(chat_on, monkeypatch):
    def boom(url, json, timeout):
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(chat.httpx, "post", boom)
    ok, detail = chat.post("slack", "hi")
    assert ok is False and "ConnectTimeout" in detail and "hooks.slack" not in detail


def test_plain_http_url_is_refused(chat_on, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://hooks.slack.example/x")
    assert chat.post("slack", "hi") == (False, "SLACK_WEBHOOK_URL must be an https:// URL")


def test_resolved_sweep_only_for_incidents_that_were_posted(chat_on):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    posted = _incident(db, "CRITICAL")
    quiet = _incident(db, "CRITICAL")
    chat.queue_step(db, posted, "immediate", "team", 0)
    for row in (posted, quiet):
        row.status = "RESOLVED"
        row.resolved_at = utcnow()
    db.commit()
    chat.sweep_resolved(db)
    keys = {n.step_key for n in db.query(Notification).filter_by(incident_id=posted.id)}
    assert {"chat:slack:resolved", "chat:discord:resolved"} <= keys
    assert db.query(Notification).filter_by(incident_id=quiet.id).count() == 0
    before = db.query(Notification).count()
    chat.sweep_resolved(db)
    assert db.query(Notification).count() == before
    db.close()


def test_settings_cli_and_doctor_know_the_chat_secrets():
    from app.cli_ops import SETTABLE_SECRETS
    from app.system_settings import BY_KEY

    for key in ("SLACK_WEBHOOK_URL", "TEAMS_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        assert key in SETTABLE_SECRETS and BY_KEY[key].store == "secret"
    assert BY_KEY["notifications.chat.slack.enabled"].test == "slack"
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "SLACK_WEBHOOK_URL=" in (root / "secrets" / "secrets.example.env").read_text()
    assert "TEAMS_WEBHOOK_URL" in (root / "scripts" / "install.sh").read_text()
    from app.stack import component_label

    assert component_label("chat").startswith("Chat")
