"""Settings › Messages and Reports previews: real builders, nothing sent; Send now is a job."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, Job, Notification, ScheduledReport, User, utcnow
from app.security import hash_password
from app.seed import seed


def _client(role: str = "engineer") -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def _incident() -> str:
    db = SessionLocal()
    asset = Asset(asset_id=f"PV-{uuid4().hex[:6]}", hostname=f"pv-{uuid4().hex[:6]}", ip="10.9.9.9", type="Linux Server", owner_email="owner@dc.local")
    db.add(asset)
    db.flush()
    row = Incident(number=f"INC-P{uuid4().hex[:6]}", title="Disk /var 95%", severity="CRITICAL", status="OPEN",
                   fingerprint=f"pv:{uuid4().hex}", asset_id=asset.id, started_at=utcnow() - timedelta(minutes=3))
    db.add(row)
    db.commit()
    number = row.number
    db.close()
    return number


def test_previews_render_mail_chat_and_report_without_sending(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**(settings.yaml or {}), "system": {"public_url": "https://noc.example"}})
    client = _client("viewer")
    number = _incident()
    db = SessionLocal()
    before = db.query(Notification).count()
    db.close()
    data = client.get(f"/api/v1/message-previews?incident={number}").json()
    assert data["incident"] == number and data["note"] == "" and data["public_url"] == "https://noc.example"
    assert data["alert_email"]["subject"].startswith("Alarm pv-")
    assert "Acknowledge this incident" in data["alert_email"]["html"]
    slack = data["chat"]["events"]["opened"]["payloads"]["slack"]
    assert slack["attachments"][0]["blocks"][0]["text"]["text"] == "*[CRITICAL] Opened: Disk /var 95%*"
    assert slack["attachments"][0]["blocks"][2]["elements"][0]["url"] == f"https://noc.example/app/incidents/{number}"
    assert set(data["chat"]["events"]) == {"opened", "escalated", "resolved"}
    assert "<html" in data["incident_report"]["html"]
    db = SessionLocal()
    assert db.query(Notification).count() == before
    db.close()


def test_scheduled_report_preview_has_the_summary_and_send_now_is_a_job():
    client = _client("engineer")
    _incident()
    sample = client.get("/api/v1/scheduled-reports/preview").json()
    assert "at a glance" in sample["html"] and "Incidents per day" in sample["html"] and "Still open" in sample["html"]
    assert "Last 7 days" in sample["text"]
    db = SessionLocal()
    row = ScheduledReport(name=f"weekly-{uuid4().hex[:4]}", to_email="noc@dc.local", enabled=True,
                          schedule={"repeat": True, "cron": "0 8 * * 1"})
    db.add(row)
    db.commit()
    rid = row.id
    db.close()
    one = client.get(f"/api/v1/scheduled-reports/{rid}/preview").json()
    assert one["recipients"] == ["noc@dc.local"] and "The last 7 days at a glance" in one["html"]
    assert client.post(f"/api/v1/scheduled-reports/{rid}/send", json={}).json()["queued"] is True
    db = SessionLocal()
    job = db.query(Job).filter_by(kind="report_send", object_id=str(rid)).one()
    assert job.status == "pending"
    db.delete(job)
    db.commit()
    db.close()
    assert _client("viewer").post(f"/api/v1/scheduled-reports/{rid}/send", json={}).status_code == 403


def test_report_days_follow_the_schedule():
    from app.message_previews import report_days

    def row(schedule):
        return ScheduledReport(name="x", to_email="a@b.c", schedule=schedule)

    assert report_days(row({"repeat": True, "cron": "0 7 * * *"})) == 1
    assert report_days(row({"repeat": True, "cron": "0 7 * * 1"})) == 7
    assert report_days(row({"repeat": True, "cron": "0 8 1 * *"})) == 30
    assert report_days(row({"repeat": True, "every_minutes": 1440, "preset": "24h"})) == 1
    assert report_days(None) == 7
