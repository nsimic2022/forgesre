"""Incident page: who to call, what happened before on the asset, the merged story, syslog."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, IncidentNote, Notification, User, utcnow
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


def test_view_has_people_history_and_one_story(monkeypatch):
    import app.dc_noc as dc_noc

    monkeypatch.setattr(dc_noc, "current_oncall", lambda db, at=None: {"name": "Ana", "phone": "+381 60 1", "email": "ana@dc.local"})
    client = _client("viewer")
    db = SessionLocal()
    host = f"db-{uuid4().hex[:6]}"
    asset = Asset(asset_id=host, hostname=host, ip="10.5.5.5", type="Linux Server", owner="DBA", contact_name="Marko",
                  owner_phone="+381 60 2", owner_email="dba@dc.local", extras={"backup_name": "Jovan", "backup_phone": "+381 60 3"})
    db.add(asset)
    db.flush()
    old = Incident(number=f"INC-O{uuid4().hex[:6]}", title="Disk /u02 92%", severity="HIGH", status="RESOLVED", fingerprint=f"o:{uuid4().hex}",
                   asset_id=asset.id, started_at=utcnow() - timedelta(days=3), resolved_at=utcnow() - timedelta(days=3) + timedelta(minutes=40),
                   root_cause="Archive logs not purged")
    now = Incident(number=f"INC-N{uuid4().hex[:6]}", title="Disk /u02 92%", severity="HIGH", status="INVESTIGATING", fingerprint=f"n:{uuid4().hex}",
                   asset_id=asset.id, started_at=utcnow() - timedelta(minutes=10), ack_at=utcnow() - timedelta(minutes=5), ack_by="ana@dc.local")
    db.add_all([old, now])
    db.flush()
    db.add(Notification(incident_id=now.id, channel="email", target="dba@dc.local", subject="Alarm", body="", status="sent", step_key="immediate"))
    db.add(IncidentNote(incident_id=now.id, actor="ana@dc.local", body="Purging archive logs"))
    db.commit()
    number, old_number = now.number, old.number
    db.close()
    data = client.get(f"/api/v1/incidents/{number}/view").json()
    roles = [p["role"] for p in data["who_to_call"]]
    assert roles[0] == "On call now" and roles[1].startswith("Asset contact") and "Backup contact" in roles
    assert data["who_to_call"][1]["phone"] == "+381 60 2"
    assert data["similar"][0]["number"] == old_number and data["similar"][0]["root_cause"] == "Archive logs not purged"
    assert data["similar"][0]["minutes"] == 40
    kinds = [e["kind"] for e in data["events"]]
    assert kinds[0] == "alarm" and {"notify", "note", "ack"} <= set(kinds)
    assert [e["at"] for e in data["events"]] == sorted(e["at"] for e in data["events"])


def test_syslog_needs_evidence_rights(monkeypatch):
    import app.dc_noc as dc_noc

    monkeypatch.setattr(dc_noc, "incident_syslog_lines", lambda incident: ["link down"])
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    seed(db)
    row = Incident(number=f"INC-S{uuid4().hex[:6]}", title="x", severity="HIGH", status="OPEN", fingerprint=f"s:{uuid4().hex}", started_at=utcnow())
    db.add(row)
    db.commit()
    number = row.number
    db.close()
    assert _client("viewer").get(f"/api/v1/incidents/{number}/syslog").status_code == 403
    assert _client("analyst").get(f"/api/v1/incidents/{number}/syslog").json()["lines"] == ["link down"]
