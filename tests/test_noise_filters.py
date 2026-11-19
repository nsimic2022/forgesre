"""Noise filters: matching rules, quiet intake, promotion when they stop applying, API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, NoiseFilter, Notification, User
from app.noise_filters import filter_applies, filter_id_of, promote_filtered
from app.security import hash_password
from app.seed import seed
from app.services import ingest_alertmanager


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _flt(**kw) -> NoiseFilter:
    base = dict(name="t", enabled=True, alertnames=[], scope="all", scope_value="", days=[], start_minute=None,
                end_minute=None, max_value=None, action="quiet", delay_minutes=5, expires_at=None)
    base.update(kw)
    return NoiseFilter(**base)


def _at(local: str) -> datetime:
    from app.dc_noc import _zone

    return datetime.fromisoformat(local).replace(tzinfo=_zone())


ASSET = SimpleNamespace(hostname="db-ora-02", asset_id="A-1", ip="10.0.0.2", type="Oracle DB", extras={"site": "DC Beograd"})


def test_window_crossing_midnight_days_and_value():
    backup = _flt(alertnames=["DiskUsage"], scope="type", scope_value="oracle db", start_minute=23 * 60, end_minute=3 * 60, max_value=97)
    monday_night = _at("2026-11-16T23:30")
    assert filter_applies(backup, "DiskUsage", ASSET, 96, monday_night)
    assert filter_applies(backup, "diskusage", ASSET, None, _at("2026-11-17T02:59"))
    assert not filter_applies(backup, "DiskUsage", ASSET, 96, _at("2026-11-17T03:00"))
    assert not filter_applies(backup, "DiskUsage", ASSET, 99, monday_night)  # above max: real
    assert not filter_applies(backup, "HighCPU", ASSET, 96, monday_night)
    weekend = _flt(days=[5, 6])
    assert filter_applies(weekend, "X", None, None, _at("2026-11-21T12:00"))  # Saturday
    assert not filter_applies(weekend, "X", None, None, _at("2026-11-20T12:00"))


def test_scope_expiry_and_delay():
    site = _flt(scope="site", scope_value="DC Beograd")
    now = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)
    assert filter_applies(site, "X", ASSET, None, now) and not filter_applies(site, "X", None, None, now)
    assert not filter_applies(_flt(expires_at=now - timedelta(minutes=1)), "X", None, None, now)
    assert not filter_applies(_flt(enabled=False), "X", None, None, now)
    spike = _flt(action="delay", delay_minutes=5)
    assert filter_applies(spike, "X", None, None, now, started_at=now - timedelta(minutes=4))
    assert not filter_applies(spike, "X", None, None, now, started_at=now - timedelta(minutes=6))


def _alert(name: str, host: str, value: str = "") -> dict:
    labels = {"alertname": name, "asset": host, "instance": f"{host}:9100", "severity": "warning"}
    return {"status": "firing", "alerts": [{"status": "firing", "labels": labels, "annotations": {"summary": name, "value": value}, "startsAt": datetime.now(timezone.utc).isoformat()}]}


def test_filtered_alarm_is_recorded_quietly_then_opens_when_filter_stops():
    db = _db()
    tag = uuid4().hex[:6]
    asset = Asset(asset_id=f"NF-{tag}", hostname=f"nf-{tag}", ip="10.66.0.1", type="Linux Server")
    db.add(asset)
    flt = _flt(name=f"spikes-{tag}", alertnames=[f"NoiseTest{tag}"], scope="asset", scope_value=f"nf-{tag}")
    db.add(flt)
    db.commit()

    ingest_alertmanager(db, _alert(f"NoiseTest{tag}", f"nf-{tag}", "91"))
    incident = db.query(Incident).filter(Incident.asset_id == asset.id).order_by(Incident.id.desc()).first()
    assert incident is not None and incident.status == "SUPPRESSED"
    assert filter_id_of(incident) == flt.id
    assert any("recorded, nobody notified" in str(t.get("detail")) for t in incident.timeline)
    assert db.query(Notification).filter_by(incident_id=incident.id).count() == 0
    db.refresh(flt)
    assert flt.hits == 1 and flt.last_hit_at is not None

    assert promote_filtered(db) == 0  # still applies
    flt.enabled = False
    db.commit()
    assert promote_filtered(db) >= 1
    db.refresh(incident)
    assert incident.status == "OPEN"
    assert any(t.get("id") == "quiet-clear" for t in incident.timeline)
    assert filter_id_of(incident) is None
    db.close()


def _client(role: str) -> TestClient:
    db = _db()
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def test_api_crud_validation_and_simulator():
    eng = _client("engineer")
    body = {"name": "Backup window", "alertnames": ["DiskUsage"], "scope": "type", "scope_value": "Linux Server",
            "start_minute": 0, "end_minute": 1439, "action": "quiet", "owner": "dba", "note": "nightly backup"}
    created = eng.post("/api/v1/noise-filters", json=body)
    assert created.status_code == 200, created.text
    fid = created.json()["id"]
    assert eng.post("/api/v1/noise-filters", json={**body, "scope_value": ""}).status_code == 400
    assert eng.post("/api/v1/noise-filters", json={"name": "forever", "action": "quiet"}).status_code == 400
    assert eng.post("/api/v1/noise-filters", json={"name": "forever", "action": "quiet", "confirm_always": True}).status_code == 200
    updated = eng.put(f"/api/v1/noise-filters/{fid}", json={**body, "max_value": 97})
    assert updated.json()["max_value"] == 97

    db = _db()
    tag = uuid4().hex[:6]
    db.add(Asset(asset_id=f"SIM-{tag}", hostname=f"sim-{tag}", ip="10.65.0.1", type="Linux Server"))
    db.commit()
    db.close()
    sim = eng.post("/api/v1/automation/simulate", json={"alertname": "DiskUsage", "asset": f"sim-{tag}", "value": "96"}).json()
    assert sim["outcome"] == "filtered" and sim["filter"] == "Backup window"
    over = eng.post("/api/v1/automation/simulate", json={"alertname": "DiskUsage", "asset": f"sim-{tag}", "value": "99"}).json()
    assert over["outcome"] != "filtered" or over.get("filter") != "Backup window"
    assert any(f["name"] == "Backup window" for f in eng.get("/api/v1/automation").json()["filters"])

    analyst = _client("analyst")
    assert analyst.get("/api/v1/noise-filters").status_code == 200
    assert analyst.post("/api/v1/noise-filters", json=body).status_code == 403
    assert eng.delete(f"/api/v1/noise-filters/{fid}").json() == {"deleted": fid}
    for row in eng.get("/api/v1/noise-filters").json():
        eng.delete(f"/api/v1/noise-filters/{row['id']}")
