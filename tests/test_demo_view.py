"""Settings › Demo: scenarios run as the demo_run job, Admin only."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Incident, User
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


def test_demo_scenario_opens_a_lab_incident_in_a_job():
    from app.jobs import run_pending_jobs

    admin = _client("admin")
    keys = [s["key"] for s in admin.get("/api/v1/demo/scenarios").json()["scenarios"]]
    assert keys == ["highcpu", "disk", "host", "nodecpu", "windows", "network"]
    assert admin.post("/api/v1/demo/run", json={"scenario": "rm -rf"}).status_code == 400
    job = admin.post("/api/v1/demo/run", json={"scenario": "network"}).json()["job"]
    assert admin.get(f"/api/v1/demo/jobs/{job}").json()["status"] == "pending"
    db = SessionLocal()
    run_pending_jobs(db, limit=10)
    done = admin.get(f"/api/v1/demo/jobs/{job}").json()
    assert done["status"] == "done", done
    incident = db.query(Incident).filter_by(number=done["result"]["incident"]).one()
    assert "forge-demo-sw-01" in incident.fingerprint
    db.close()


def test_demo_is_admin_only():
    engineer = _client("engineer")
    assert engineer.get("/api/v1/demo/scenarios").status_code == 403
    assert engineer.post("/api/v1/demo/run", json={"scenario": "highcpu"}).status_code == 403
