"""Notifications flow: escalation and on-call blocks, e-mail lane (no mail sent by the check), chat
lane, web push, and the send log with masked recipients."""

from __future__ import annotations

import socket
import threading

import pytest

from app.db import Base, SessionLocal, engine
from app.models import Incident, IntegrationExchange, Notification, PushSubscription, utcnow
from app.settings import settings


@pytest.fixture
def db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    session.query(IntegrationExchange).filter_by(integration="notifications").delete()
    session.commit()
    yield session
    session.query(Notification).filter(Notification.subject.like("nf-%")).delete(synchronize_session=False)
    session.query(PushSubscription).filter(PushSubscription.email.like("nf-%")).delete(synchronize_session=False)
    session.query(Incident).filter(Incident.number.like("INC-NF%")).delete(synchronize_session=False)
    session.commit()
    session.close()


def _listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    return srv


def _steps(result, lane):
    return {s["id"]: s for s in next(x for x in result["lanes"] if x["id"] == lane)["steps"]}


def test_pipeline_email_chat_and_push_lanes(db, monkeypatch):
    from app import chat, email_service, flow_notifications, web_push
    from app.services import NO_RECIPIENT_STATUS

    srv = _listener()
    port = srv.getsockname()[1]
    cls = type(settings)
    monkeypatch.setattr(cls, "email_enabled", property(lambda s: True))
    monkeypatch.setattr(cls, "smtp_host", property(lambda s: "127.0.0.1"))
    monkeypatch.setattr(cls, "smtp_port", property(lambda s: port))
    sent: list[bool] = []
    monkeypatch.setattr(email_service, "check", lambda send=True: sent.append(send) or {"status": "error", "why": "127.0.0.1 refused the sign-in (535)", "fix": "check SMTP_USERNAME"})
    monkeypatch.setattr(chat, "channel_config", lambda c: {"enabled": c == "slack"})
    monkeypatch.setattr(chat, "webhook_url", lambda c: f"https://127.0.0.1:{port}/services/SECRET" if c == "slack" else "")
    monkeypatch.setattr(web_push, "enabled", lambda: True)
    inc = Incident(number="INC-NF1", title="nf disk", severity="HIGH", status="OPEN", fingerprint="nf1", started_at=utcnow())
    db.add(inc)
    db.flush()
    db.add_all([Notification(incident_id=inc.id, channel="email", target="oncall", subject="nf-a", status=NO_RECIPIENT_STATUS, step_key="15m", error="no recipient"),
                Notification(incident_id=inc.id, channel="email", target="ana.ops@example.com", subject="nf-b", status="failed", step_key="immediate", error="535 auth"),
                Notification(incident_id=inc.id, channel="slack", target="slack", subject="nf-c", status="sent", step_key="chat:immediate"),
                PushSubscription(email="nf-ana@example.com", endpoint="https://push.example/nf1", p256dh="x", auth="y", fails=2, last_error="error:403")])
    db.commit()
    try:
        result = flow_notifications.check(db)
    finally:
        srv.close()
    pipe = _steps(result, "in")
    assert pipe["escalation"]["status"] == "bad" and "had nobody to send to" in pipe["escalation"]["detail"]
    assert pipe["channels"]["status"] == "ok" and "e-mail, chat, web push" in pipe["channels"]["detail"]
    mail = _steps(result, "email")
    assert mail["smtp_dns"]["status"] == "ok" and mail["smtp_tcp"]["status"] == "ok" and "smtp_tls" not in mail
    assert mail["smtp_login"]["status"] == "bad" and sent == [False]  # signed in only, never sent a mail
    assert mail["mail_sent"]["status"] == "bad" and "a…@example.com" in mail["mail_sent"]["rows"][0]["subject"]
    talk = _steps(result, "chat")
    assert talk["slack_cfg"]["status"] == "ok" and "SECRET" not in str(talk)
    assert talk["slack_tls"]["status"] == "bad"
    # Older failed rows from other tests may make it "warn"; the newest row is ours.
    assert talk["slack_sent"]["status"] in ("ok", "warn") and talk["slack_sent"]["rows"][0]["status"] == "sent"
    push = _steps(result, "push")
    assert push["push_subs"]["status"] == "warn" and "1 failing" in push["push_subs"]["detail"]


def test_sends_are_logged_with_masked_recipients(db, monkeypatch):
    import httpx

    from app import chat, email_service

    monkeypatch.setattr(email_service, "send_smtp_async", lambda *a, **k: _done())
    email_service.send_smtp("maria.k@example.org", "nf-subject", "body")
    monkeypatch.setattr(chat, "webhook_url", lambda c: "https://hooks.slack.test/services/T0/B0/SECRET")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200))
    assert chat.post("slack", "nf test")[0]
    rows = db.query(IntegrationExchange).filter_by(integration="notifications").order_by(IntegrationExchange.id).all()
    assert [r.kind for r in rows] == ["email", "chat.slack"]
    assert "m…@example.org" in rows[0].summary and "maria" not in rows[0].summary
    assert rows[1].peer == "hooks.slack.test" and "SECRET" not in rows[1].summary + rows[1].peer


async def _done():
    return None


def test_flow_is_registered_and_listed():
    from app.flows import flows

    assert list(flows())[:3] == ["prometheus", "zabbix", "notifications"]
