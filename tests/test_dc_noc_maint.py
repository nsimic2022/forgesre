"""Maintenance windows: record the incident, do not start the mail ladder."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import in_maintenance, maint_pair_error
from app.inventory import create_manual_asset
from app.main import app
from app.models import Asset, Incident, Notification, utcnow
from app.seed import seed
from app.services import ingest_alertmanager, process_escalations


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _asset(db, token: str, **extras) -> Asset:
    row = create_manual_asset(
        db,
        hostname=f"maint-{token}",
        asset_id=f"maint-{token}",
        ip=f"10.77.{int(token[:2], 16) % 200 + 1}.{int(token[2:4], 16) % 200 + 1}",
        type="Linux Server",
        owner_email="maint-owner@dc.local",
        extras=extras or None,
        actor="tester",
    )
    return row


def _alert(asset_id: str, name: str) -> dict:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": name, "severity": "warning", "asset": asset_id, "instance": asset_id},
                "annotations": {"summary": name, "description": "maintenance test"},
            }
        ],
    }


def _mails(db, incident: Incident) -> list[Notification]:
    return db.query(Notification).filter(Notification.incident_id == incident.id).all()


def test_maint_pair_rejects_half_window():
    assert maint_pair_error("", "") == ""
    assert "both" in maint_pair_error("2026-10-13T10:00", "").lower()
    assert "after" in maint_pair_error("2026-10-13T12:00", "2026-10-13T10:00").lower()


def test_in_window_skips_mail_and_after_window_mails():
    db = _db()
    token = uuid4().hex[:8]
    now = utcnow()
    asset = _asset(
        db,
        token,
        maint_from=(now - timedelta(hours=1)).isoformat(),
        maint_until=(now + timedelta(hours=1)).isoformat(),
    )
    db.refresh(asset)
    assert in_maintenance(asset, now)
    name = f"MaintProbe{token}"
    opened = ingest_alertmanager(db, _alert(asset.asset_id, name))
    assert len(opened) == 1
    incident = opened[0]
    db.refresh(incident)
    assert incident.status == "MAINT"
    assert _mails(db, incident) == []
    incident.started_at = now - timedelta(hours=2)
    db.commit()
    process_escalations(db)
    assert _mails(db, incident) == []

    asset.extras = {}
    db.commit()
    db.refresh(asset)
    assert not in_maintenance(asset, utcnow())
    again = ingest_alertmanager(db, _alert(asset.asset_id, name))
    assert again == []
    db.refresh(incident)
    assert incident.status == "OPEN"
    mails = _mails(db, incident)
    assert len(mails) == 1
    assert mails[0].target == "maint-owner@dc.local"
    assert mails[0].status in {"generated", "sent"}
    db.close()


def test_asset_list_badge_and_form_fields():
    db = _db()
    token = uuid4().hex[:8]
    now = utcnow()
    asset = _asset(
        db,
        token,
        maint_from=(now - timedelta(minutes=5)).isoformat(),
        maint_until=(now + timedelta(hours=2)).isoformat(),
    )
    client = _client()
    page = client.get("/assets")
    assert page.status_code == 200
    assert 'name="extra_maint_from"' in page.text
    assert 'name="extra_maint_until"' in page.text
    assert "Standard-alarm mute" in page.text or "not Standard-alarm mute" in page.text
    listed = client.get(f"/assets?q={asset.asset_id}")
    assert "data-maint-badge" in listed.text
    assert "Maintenance" in listed.text
    detail = client.get(f"/assets/{asset.asset_id}")
    assert 'data-maintenance' in detail.text
    assert "data-maint-badge" in detail.text
    db.close()
