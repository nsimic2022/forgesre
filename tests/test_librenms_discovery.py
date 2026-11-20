"""Discovery comes from LibreNMS: known devices update assets, new ones are listed or added, ForgeSRE never sweeps."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app import librenms, librenms_discovery
from app.db import Base, SessionLocal, engine
from app.jobs import DISCOVERY_SCAN_KIND, run_pending_jobs
from app.librenms_discovery import apply_devices, role_for
from app.main import app
from app.models import Asset, DiscoveryCandidate, Job
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _net(tag: str) -> str:
    return f"10.{90 + int(tag[:2], 16) % 60}.{int(tag[2:4], 16)}"


def test_role_for_maps_librenms_types():
    assert role_for({"type": "network", "os": "ios"}) == "Network device"
    assert role_for({"type": "firewall"}) == "Firewall"
    assert role_for({"type": "printer"}) == "Printer"
    assert role_for({"type": "server", "os": "windows"}).startswith("Windows Server (no windows_exporter")
    assert role_for({"type": "server", "os": "linux"}).startswith("Linux Server (no node_exporter")
    assert role_for({}) == "Network device"


def test_semi_automatic_lists_new_devices_and_keeps_ignored_ones():
    db = _db()
    tag = uuid4().hex[:6]
    net = _net(tag)
    known = Asset(asset_id=f"lnmsknown{tag}", hostname=f"lnmsknown{tag}", ip=f"{net}.1", type="Switch")
    db.add(known)
    db.add(DiscoveryCandidate(ip=f"{net}.4", proposed_role="Network device", status="ignored", source="librenms"))
    db.commit()
    devices = [
        {"device_id": 1, "hostname": f"{net}.1", "sysName": f"lnmsknown{tag}", "serial": f"SN{tag}"},
        {"device_id": 2, "hostname": f"{net}.2", "ip": f"{net}.2", "sysName": f"edge{tag}", "type": "network"},
        {"device_id": 3, "hostname": f"prn{tag}.dc.local", "ip": f"{net}.3", "type": "printer"},
        {"device_id": 4, "hostname": f"{net}.4", "type": "network"},
        {"device_id": 5, "hostname": f"noip{tag}.dc.local", "type": "network"},
    ]
    counts = apply_devices(db, devices, mode="semi-automatic")
    db.commit()
    assert counts == {"known": 1, "found": 2, "added": 0, "ignored": 1, "skipped": 1}
    rows = {r.ip: r for r in db.query(DiscoveryCandidate).filter(DiscoveryCandidate.ip.like(f"{net}.%")).all()}
    assert rows[f"{net}.2"].source == "librenms" and rows[f"{net}.2"].hostname == f"edge{tag}" and rows[f"{net}.2"].status == "new"
    assert rows[f"{net}.3"].proposed_role == "Printer"
    assert rows[f"{net}.4"].status == "ignored"
    assert db.query(Asset).filter(Asset.ip.like(f"{net}.%")).count() == 1  # nothing added without an Engineer
    db.close()


def test_automatic_adds_new_devices_as_librenms_assets():
    db = _db()
    tag = uuid4().hex[:6]
    net = _net(tag)
    counts = apply_devices(db, [{"device_id": 7, "hostname": f"fw{tag}", "ip": f"{net}.7", "sysName": f"fw{tag}", "type": "firewall"}], mode="automatic")
    db.commit()
    assert counts["added"] == 1
    asset = db.query(Asset).filter_by(ip=f"{net}.7").one()
    assert (asset.type, asset.source, asset.hostname) == ("Firewall", "librenms", f"fw{tag}")
    db.close()


def test_read_now_needs_librenms_and_runs_as_a_job(monkeypatch):
    db = _db()
    db.query(Job).filter(Job.kind == DISCOVERY_SCAN_KIND, Job.status.in_(["pending", "running"])).update({"status": "done"})
    db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})
    monkeypatch.setattr(librenms_discovery, "not_ready", lambda: "LibreNMS has no API token yet")
    refused = client.post("/api/v1/discovery/scan")
    assert refused.status_code == 400 and "LibreNMS" in refused.json()["detail"]
    status = client.get("/api/v1/discovery/status").json()
    assert status["ready"] is False and "cidrs" not in status

    tag = uuid4().hex[:6]
    net = _net(tag)

    async def fake_devices(**kw):
        return [{"device_id": 11, "hostname": f"sw{tag}", "ip": f"{net}.11", "type": "network"}]

    async def no_links(method, path, **kw):
        return {"links": []}

    monkeypatch.setattr(librenms_discovery, "not_ready", lambda: "")
    monkeypatch.setattr(librenms, "list_devices", fake_devices)
    monkeypatch.setattr(librenms, "_call", no_links)
    queued = client.post("/api/v1/discovery/scan")
    assert queued.status_code == 200 and queued.json()["kind"] == "discovery_scan"
    job_id = queued.json()["job_id"]
    # Other tests share this database; park their pending jobs so only the read runs.
    others = db.query(Job).filter(Job.status == "pending", Job.id != job_id).all()
    for row in others:
        row.status = "parked"
    db.commit()
    try:
        run_pending_jobs(db)
    finally:
        for row in others:
            row.status = "pending"
        db.commit()
    db.expire_all()
    job = db.get(Job, job_id)
    assert job.status == "done", job.error
    assert job.payload["result"]["found"] == 1
    assert db.query(DiscoveryCandidate).filter_by(ip=f"{net}.11", source="librenms").count() == 1
    last = client.get("/api/v1/discovery/status").json()["last"]
    assert last["status"] == "done" and last["result"]["devices"] == 1
    db.close()
