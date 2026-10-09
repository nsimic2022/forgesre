"""Filtered-audit quick wins: report mode 600, JSON ack, escalated count, handbook."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Incident
from app.seed import seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _incident(db, status: str) -> Incident:
    row = Incident(
        number=next_incident_number(db),
        title=f"quick win {status} {uuid4().hex[:6]}",
        severity="WARNING",
        status=status,
        fingerprint=f"qw:{uuid4().hex}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_api_investigating_on_escalated_acknowledges_without_demotion():
    db = _db()
    client = _client()
    row = _incident(db, "ESCALATED")
    out = client.post(f"/api/v1/incidents/{row.number}/status", json={"status": "INVESTIGATING"})
    assert out.status_code == 200
    body = out.json()
    assert body["status"] == "ESCALATED"
    assert body["ack_at"]
    assert body["ack_by"] == "admin@forgesre.local"
    db.expire_all()
    fresh = db.query(Incident).filter_by(number=row.number).one()
    assert fresh.status == "ESCALATED"
    assert fresh.ack_at is not None
    assert fresh.ack_by == "admin@forgesre.local"
    db.close()


def test_api_open_does_not_demote_escalated():
    db = _db()
    client = _client()
    row = _incident(db, "ESCALATED")
    out = client.post(f"/api/v1/incidents/{row.number}/status", json={"status": "OPEN"})
    assert out.status_code == 200
    assert out.json()["status"] == "ESCALATED"
    assert out.json()["ack_at"] is None
    db.expire_all()
    fresh = db.query(Incident).filter_by(number=row.number).one()
    assert fresh.status == "ESCALATED"
    assert fresh.ack_at is None
    db.close()


def test_api_acknowledge_moves_open_to_investigating():
    db = _db()
    client = _client()
    row = _incident(db, "OPEN")
    out = client.post(f"/api/v1/incidents/{row.number}/status", json={"status": "investigating"})
    assert out.status_code == 200
    assert out.json()["status"] == "INVESTIGATING"
    assert out.json()["ack_at"]
    db.expire_all()
    fresh = db.query(Incident).filter_by(number=row.number).one()
    assert fresh.status == "INVESTIGATING"
    assert fresh.ack_at is not None
    db.close()


def test_api_resolved_and_closed_still_apply_on_escalated():
    db = _db()
    client = _client()
    for target in ("RESOLVED", "CLOSED"):
        row = _incident(db, "ESCALATED")
        out = client.post(f"/api/v1/incidents/{row.number}/status", json={"status": target})
        assert out.status_code == 200
        assert out.json()["status"] == target
        db.expire_all()
        fresh = db.query(Incident).filter_by(number=row.number).one()
        assert fresh.status == target
        assert fresh.resolved_at is not None
    db.close()


def test_system_status_counts_escalated_apart_from_open(monkeypatch):
    monkeypatch.setattr("app.api.doctor_payload", lambda **kwargs: {"components": {"core": {"status": "ok"}}})
    db = _db()
    client = _client()
    for status in ("OPEN", "INVESTIGATING", "ESCALATED", "RESOLVED"):
        _incident(db, status)
    counts = dict(db.query(Incident.status, func.count(Incident.id)).group_by(Incident.status).all())
    out = client.get("/api/v1/system/status")
    assert out.status_code == 200
    incidents = out.json()["incidents"]
    assert "escalated" in incidents
    assert incidents["escalated"] == int(counts.get("ESCALATED") or 0)
    assert incidents["open"] == int(counts.get("OPEN") or 0)
    assert incidents["investigating"] == int(counts.get("INVESTIGATING") or 0)
    assert incidents["resolved"] == int(counts.get("RESOLVED") or 0)
    assert incidents["escalated"] >= 1
    assert incidents["open"] >= 1
    db.close()


def test_handbook_report_mode_escalated_cube_and_investigating_tip():
    script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    start = script.index('cat > "$ROOT/installation-report.md"')
    block = script[start:].split("\nstart_stack()", 1)[0]
    assert "Admin password: ${admin_pass}" in block
    assert 'chmod 600 "$ROOT/installation-report.md"' in block
    assert block.index("EOF") < block.index('chmod 600 "$ROOT/installation-report.md"')

    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    passwords = [line for line in handbook.splitlines() if "installation-report.md" in line and "mode `600`" in line]
    assert passwords
    assert "do not show an Open or Escalated cube" not in handbook
    assert "Dashboard tiles show an **Escalated** cube" in handbook
    assert "do not show an Open cube" in handbook
    assert "The Dashboard alarm row includes **Escalated**" in handbook

    dash = (ROOT / "frontend" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    tip = dash.split("Alarm counts", 1)[1].split("{% endset %}", 1)[0]
    assert "RCA work in progress" not in tip
    assert "only Acknowledge sets this" in tip
    assert "escalated incident stays ESCALATED" in tip
    assert "RCA does not set INVESTIGATING" in tip
