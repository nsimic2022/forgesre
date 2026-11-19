"""Dependency map: LibreNMS links → parent suggestions → tree; parent changes from the new UI."""

from __future__ import annotations

from uuid import uuid4

from app import librenms
from app.db import Base, SessionLocal, engine
from app.models import Asset, TopologyLink
from app.topology import suggestions, sync_librenms


def _assets(db, tag):
    rows = {
        "sw": Asset(asset_id=f"sw-{tag}", hostname=f"sw-{tag}", ip=f"10.77.{len(tag)}.1", type="Switch"),
        "core": Asset(asset_id=f"core-{tag}", hostname=f"core-{tag}", ip="10.77.0.254", type="Router"),
        "srv": Asset(asset_id=f"srv-{tag}", hostname=f"srv-{tag}.dc.local", ip="10.77.9.9", type="Linux Server"),
    }
    db.add_all(rows.values())
    db.commit()
    return rows


def test_sync_matches_assets_and_proposes_only_safe_parents(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    a = _assets(db, tag)
    devices = [{"device_id": 1, "hostname": a["sw"].ip, "sysName": f"sw-{tag}"},
               {"device_id": 2, "hostname": f"core-{tag}", "sysName": ""}]
    links = [
        {"local_device_id": 1, "remote_device_id": 0, "remote_hostname": f"srv-{tag}", "protocol": "lldp", "remote_port": "eth0", "local_port_id": 14},
        {"local_device_id": 1, "remote_device_id": 2, "remote_hostname": f"core-{tag}", "protocol": "cdp", "remote_port": "Te1/1", "local_port_id": 48},
        {"local_device_id": 2, "remote_device_id": 0, "remote_hostname": "unknown-box", "protocol": "lldp", "remote_port": "x", "local_port_id": 1},
    ]

    async def fake_devices(**kw):
        return devices

    async def fake_call(method, path, **kw):
        assert path == "/resources/links"
        return {"links": links}

    monkeypatch.setattr(librenms, "list_devices", fake_devices)
    monkeypatch.setattr(librenms, "_call", fake_call)
    result = sync_librenms(db)
    assert result["links"] == 3 and result["matched"] == 2
    mine = [s for s in suggestions(db) if s["asset_id"] == f"srv-{tag}"]
    assert mine == [{"asset_id": f"srv-{tag}", "hostname": f"srv-{tag}.dc.local", "parent_asset_id": f"sw-{tag}",
                     "parent_hostname": f"sw-{tag}", "current_parent": "", "protocol": "lldp", "port": "eth0"}]
    assert not [s for s in suggestions(db) if s["asset_id"] in (f"sw-{tag}", f"core-{tag}")]  # switch-router not guessed

    async def no_links(method, path, **kw):
        raise librenms.LibreNmsError("Links do not exist")

    monkeypatch.setattr(librenms, "_call", no_links)
    assert sync_librenms(db)["links"] == 0
    assert db.query(TopologyLink).filter_by(source="librenms").count() == 0
    db.close()


def test_tree_and_parent_api_with_loop_guard():
    from tests.test_report_cron import _signed_in

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    a = _assets(db, tag)
    db.close()
    analyst = _signed_in("analyst")
    body = {"asset_id": f"srv-{tag}", "parent_asset_id": f"sw-{tag}"}
    assert analyst.post("/api/v1/topology/parent", json=body).status_code == 403
    assert analyst.get("/api/v1/topology").json()["can_edit"] is False
    eng = _signed_in("engineer")
    assert eng.post("/api/v1/topology/parent", json=body).status_code == 200
    assert eng.post("/api/v1/topology/parent", json={"asset_id": f"sw-{tag}", "parent_asset_id": f"core-{tag}"}).status_code == 200
    loop = eng.post("/api/v1/topology/parent", json={"asset_id": f"core-{tag}", "parent_asset_id": f"srv-{tag}"})
    assert loop.status_code == 400 and "loop" in loop.json()["detail"]
    data = eng.get("/api/v1/topology").json()
    root = next(r for r in data["roots"] if r["asset_id"] == f"core-{tag}")
    assert root["behind"] == 2 and root["children"][0]["children"][0]["asset_id"] == f"srv-{tag}"
    started = eng.post("/api/v1/topology/sync").json()
    assert started["status"] in ("pending", "running", "done")
    from app.models import Job

    db = SessionLocal()
    db.query(Job).filter_by(id=started["job"]).delete()  # never let a later test run it against no LibreNMS
    db.commit()
    db.close()
    assert eng.post("/api/v1/topology/parent", json={"asset_id": f"srv-{tag}", "parent_asset_id": ""}).status_code == 200
    db = SessionLocal()
    assert db.query(Asset).filter_by(asset_id=f"srv-{tag}").one().parent_id is None
    db.close()
