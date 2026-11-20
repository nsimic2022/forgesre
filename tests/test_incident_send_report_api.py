"""Send report from the new incident page: draft, checked addresses, one outbox row per address in a job."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, Job, Notification, User, utcnow
from app.security import hash_password
from app.seed import seed


def _client(role: str) -> TestClient:
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
    """An incident on the seeded demo host; new assets would shift asset numbers for other tests."""
    db = SessionLocal()
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    number = f"INC-M{uuid4().hex[:6]}"
    db.add(Incident(number=number, title="HighCPU", severity="HIGH", status="OPEN", fingerprint=f"HighCPU:{number}",
                    asset_id=asset.id, started_at=utcnow() - timedelta(minutes=9)))
    db.commit()
    db.close()
    return number


def test_send_report_queues_a_job_and_writes_one_outbox_row_per_address(monkeypatch):
    from app.jobs import run_pending_jobs

    sent: list[tuple[str, str, bool]] = []
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    monkeypatch.setattr("app.services._send_smtp", lambda target, subject, body, html=None: sent.append((target, body, html is not None)))
    client = _client("analyst")
    number = _incident()
    draft = client.get(f"/api/v1/incidents/{number}/mail").json()
    assert draft["subject"].startswith("Alarm ") and number in draft["body"] and draft["smtp"] is True

    bad = client.post(f"/api/v1/incidents/{number}/mail", json={"to": "ops@dc.local, nope"})
    assert bad.status_code == 400 and "nope" in bad.json()["detail"]
    assert client.post(f"/api/v1/incidents/{number}/mail", json={"to": ""}).status_code == 400
    assert sent == []

    job = client.post(f"/api/v1/incidents/{number}/mail", json={"to": "ops@dc.local; lead@dc.local, OPS@dc.local"}).json()["job"]
    assert sent == []  # nothing goes out inside the request
    edited = client.post(f"/api/v1/incidents/{number}/mail", json={"to": ["boss@dc.local"], "body": "Short note for the boss."}).json()["job"]
    db = SessionLocal()
    run_pending_jobs(db, limit=10)
    assert db.get(Job, edited).payload.get("body") is None
    rows = db.query(Notification).filter(Notification.step_key == "incident-report", Notification.target.in_(["ops@dc.local", "boss@dc.local"])).all()
    db.close()
    assert [s[0] for s in sent] == ["ops@dc.local", "lead@dc.local", "boss@dc.local"]
    assert sent[0][2] is True and sent[2][0] == "boss@dc.local" and "Short note for the boss." in sent[2][1] and sent[2][2] is False
    assert {r.status for r in rows} == {"sent"}

    status = client.get(f"/api/v1/incidents/{number}/mail/{job}").json()
    assert status["status"] == "done" and [r["to"] for r in status["result"]] == ["ops@dc.local", "lead@dc.local"]
    assert client.get(f"/api/v1/incidents/INC-other/mail/{job}").status_code == 404
    again = client.get(f"/api/v1/incidents/{number}/mail").json()
    assert {s["email"] for s in again["suggestions"][:3]} == {"boss@dc.local", "lead@dc.local", "ops@dc.local"} or again["suggestions"][0]["label"].startswith("Owner")
    assert {s["to"] for s in again["sent"]} == {"ops@dc.local", "lead@dc.local", "boss@dc.local"}


def test_viewers_cannot_send_reports():
    viewer = _client("viewer")
    number = _incident()
    assert viewer.get(f"/api/v1/incidents/{number}/mail").status_code == 403
    assert viewer.post(f"/api/v1/incidents/{number}/mail", json={"to": "ops@dc.local"}).status_code == 403
