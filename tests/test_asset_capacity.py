"""Asset capacity: read in a background job, cached for 15 minutes, readable by viewers."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Job, User
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


def _asset() -> str:
    """The seeded demo host, with no capacity jobs yet. New assets would shift asset numbers for other tests."""
    db = SessionLocal()
    db.query(Job).filter_by(kind="asset_capacity", object_id="forge-demo-01").delete()
    db.commit()
    db.close()
    return "forge-demo-01"


def test_capacity_is_read_in_a_job_then_served_from_it(monkeypatch):
    from app import dc_noc
    from app.jobs import run_pending_jobs

    calls: list[str] = []

    def fake_panel(asset, **_kw):
        calls.append(asset.asset_id)
        return {"rows": [{"label": "Disk", "state": "forecast", "note": "~12 days to full", "spark": "0,1 80,2",
                          "forecast_days": 12, "points": 700}], "start": 1.0, "end": 2.0}

    monkeypatch.setattr(dc_noc, "capacity_panel", fake_panel)
    client = _client("viewer")
    asset_id = _asset()
    first = client.get(f"/api/v1/assets/{asset_id}/capacity").json()
    assert first["state"] == "pending" and calls == []
    assert client.get(f"/api/v1/assets/{asset_id}/capacity").json()["job"] == first["job"]
    db = SessionLocal()
    run_pending_jobs(db, limit=10)
    db.close()
    ready = client.get(f"/api/v1/assets/{asset_id}/capacity").json()
    assert ready["state"] == "ready" and ready["soonest_days"] == 12 and ready["warn"] is True
    assert ready["rows"][0]["note"] == "~12 days to full"
    client.get(f"/api/v1/assets/{asset_id}/capacity")
    assert calls == [asset_id]
    assert client.get("/api/v1/assets/nope/capacity").status_code == 404


def test_stale_or_failed_capacity_is_read_again(monkeypatch):
    from app import dc_noc
    from app.jobs import run_pending_jobs
    from app.models import utcnow

    monkeypatch.setattr(dc_noc, "capacity_panel", lambda asset, **_kw: (_ for _ in ()).throw(RuntimeError("prometheus down")))
    client = _client("viewer")
    asset_id = _asset()
    client.get(f"/api/v1/assets/{asset_id}/capacity")
    db = SessionLocal()
    run_pending_jobs(db, limit=10)
    failed = client.get(f"/api/v1/assets/{asset_id}/capacity").json()
    assert failed["state"] == "error" and "prometheus down" in failed["error"]
    run_pending_jobs(db, limit=10)
    old = db.query(Job).filter_by(kind="asset_capacity", object_id=asset_id).order_by(Job.id.desc()).first()
    old.status, old.payload, old.finished_at = "done", {"result": {"rows": []}}, utcnow() - timedelta(hours=1)
    db.commit()
    db.close()
    assert client.get(f"/api/v1/assets/{asset_id}/capacity").json()["state"] == "pending"


def test_cli_prints_capacity_rows_and_marks_soon_full():
    from app.cli_ops import format_capacity

    text = format_capacity("web-01", {"state": "ready", "measured_at": "2026-11-20T08:00:00+00:00", "rows": [
        {"label": "Disk", "note": "~9 days to full", "forecast_days": 9, "points": 720},
        {"label": "Interface receive", "note": "30-day history", "forecast_days": None, "points": 720},
    ]})
    assert " ! Disk" in text and "   Interface receive" in text and "cached 15 min" in text
    assert "failed: timeout" in format_capacity("web-01", {"state": "error", "error": "timeout"})
