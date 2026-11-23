"""Routing (BGP / OSPF) over SNMP, busiest interfaces, and Oxidized config changes."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app.changes import changes_before
from app.cli_ops import format_network
from app.db import Base, SessionLocal, engine
from app.inventory import sd_snmp_routing_targets
from app.main import app
from app.models import Asset, ChangeEvent, ConfigNode, Incident, utcnow
from app.network_view import overview, routing_metrics_on
from app.oxidized import sync
from app.seed import seed
from test_incident_tools import _client

ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"Authorization": "Bearer forgesre-dev-webhook-token"}


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    raw = uuid4().int
    return f"10.{230 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    db.query(ChangeEvent).filter(ChangeEvent.source == "oxidized").delete(synchronize_session=False)
    db.query(ConfigNode).filter(ConfigNode.full_name.like("nc-%")).delete(synchronize_session=False)
    for inc in db.query(Incident).filter(Incident.title.like("nc-test%")).all():
        db.delete(inc)
    for asset in db.query(Asset).filter(Asset.asset_id.like("nc-%")).all():
        db.delete(asset)
    db.commit()
    db.close()


def test_routing_job_targets_routers_and_opt_ins():
    assert routing_metrics_on({"type": "Router"}) and routing_metrics_on({"type": "Firewall"})
    assert not routing_metrics_on({"type": "Switch"})
    assert routing_metrics_on({"type": "Switch", "extras": {"snmp_routing": "on"}})
    assert not routing_metrics_on({"type": "Router", "extras": {"snmp_routing": "off"}})
    db = _db()
    rtr_ip, sw_ip = _ip(), _ip()
    db.add_all([Asset(asset_id="nc-rtr", hostname="nc-rtr", ip=rtr_ip, type="Router"),
                Asset(asset_id="nc-sw", hostname="nc-sw", ip=sw_ip, type="Switch")])
    db.commit()
    rows = {r["labels"]["asset"]: r for r in sd_snmp_routing_targets(db)}
    assert rows["nc-rtr"]["labels"]["snmp_module"] == "routing" and "nc-sw" not in rows
    assert TestClient(app).get("/api/v1/sd/snmp-routing").status_code == 401
    assert any(i["targets"] == [rtr_ip] for i in TestClient(app).get("/api/v1/sd/snmp-routing", headers=TOKEN).json())
    db.close()


def _fake(expr: str) -> dict:
    peer = {"asset": "nc-rtr", "bgpPeerRemoteAddr": "10.0.0.2"}
    off = {"asset": "nc-rtr", "bgpPeerRemoteAddr": "10.0.0.9"}
    table = {
        "bgpPeerState": [(peer, 3), (off, 1)], "bgpPeerAdminStatus": [(peer, 2), (off, 1)], "bgpPeerRemoteAs": [(peer, 65001), (off, 65009)],
        "bgpPeerFsmEstablishedTime": [(peer, 0), (off, 0)],
        "ospfNbrState": [({"asset": "nc-rtr", "ospfNbrIpAddr": "10.0.1.2"}, 8), ({"asset": "nc-rtr", "ospfNbrIpAddr": "10.0.1.3"}, 3)],
        "ifHCInOctets": [({"asset": "nc-sw", "ifName": "Te1/1"}, 2.5e9), ({"asset": "nc-sw", "ifIndex": "7"}, 1e6)],
        "ifHCOutOctets": [({"asset": "nc-sw", "ifName": "Te1/2"}, 3e6)],
    }
    for name, rows in table.items():
        if name + "{" in expr:
            return {"series": [{"labels": labels, "value": float(v)} for labels, v in rows]}
    return {"series": []}


def test_network_overview_sorts_problems_first():
    db = _db()
    data = overview(db, vector_fn=_fake)
    assert data["bgp"][0] == {"asset": "nc-rtr", "peer": "10.0.0.2", "remote_as": 65001, "state": "active", "enabled": True, "established_s": 0, "ok": False}
    assert data["bgp"][1]["ok"] is True  # admin-off peer is not a problem
    assert data["ospf"][0]["state"] == "init" and data["ospf"][1]["state"] == "full"
    assert data["top_in"][0]["interface"] == "Te1/1" and data["top_in"][1]["interface"] == "ifIndex 7"
    text = format_network(data)
    assert "10.0.0.2" in text and "(admin off)" in text and "2.5 Gb/s" in text
    assert "Prometheus did not answer" in format_network(overview(db, vector_fn=lambda e: {"error": "down"}))
    db.close()


def test_oxidized_first_read_is_baseline_then_changes_are_recorded(monkeypatch):
    monkeypatch.setattr("app.oxidized._cfg", lambda: {"enabled": True, "url": "http://oxidized.dc.local:8888/"})
    db = _db()
    db.add(Asset(asset_id="nc-core", hostname="nc-core", ip=_ip(), type="Router"))
    db.commit()
    nodes = [{"name": "nc-core", "full_name": "nc-dc1/nc-core", "ip": "10.9.9.1", "model": "ios", "mtime": "2026-11-23 08:00:00 UTC", "last": {"status": "success"}},
             {"name": "nc-unknown", "full_name": "nc-dc1/nc-unknown", "ip": "10.9.9.2", "mtime": "2026-11-23 08:00:00 UTC", "last": {"status": "no_connection"}}]
    first = sync(db, nodes)
    assert first == {"nodes": 2, "changed": 0, "failed_backups": 1}
    nodes[0]["mtime"] = "2026-11-23 10:15:00 UTC"
    nodes[1]["mtime"] = "2026-11-23 10:20:00 UTC"
    second = sync(db, nodes)
    assert second["changed"] == 2
    rows = {r.title: r for r in db.query(ChangeEvent).filter(ChangeEvent.source == "oxidized").all()}
    core = rows["Config changed on nc-core"]
    assert core.kind == "config" and core.asset_id is not None and core.url == "http://oxidized.dc.local:8888/node/version?node_full=nc-dc1/nc-core"
    assert rows["Config changed on nc-unknown"].asset_id is None and "10.9.9.2" in rows["Config changed on nc-unknown"].detail
    assert sync(db, nodes)["changed"] == 0
    asset = db.query(Asset).filter_by(asset_id="nc-core").one()
    inc = Incident(number=f"INC-NC-{uuid4().hex[:6]}", title="nc-test down", severity="CRITICAL", status="RESOLVED", fingerprint=f"nc-{uuid4().hex}", asset_id=asset.id, started_at=utcnow())
    db.add(inc)
    db.commit()
    assert any(c["title"] == "Config changed on nc-core" for c in changes_before(db, inc))
    db.close()


def test_api_network_and_oxidized_settings():
    assert set(_client("viewer").get("/api/v1/network").json()) >= {"top_in", "bgp", "ospf", "error"}
    from app.cli_ops import SETTABLE_SECRETS
    from app.system_settings import REGISTRY

    keys = {d.key for d in REGISTRY}
    assert {"changes.oxidized.enabled", "changes.oxidized.url", "OXIDIZED_TOKEN"} <= keys and "OXIDIZED_TOKEN" in SETTABLE_SECRETS
    from app.system_settings import test_connection

    assert test_connection("oxidized")["status"] == "disabled"


def test_routing_module_job_and_rules_are_bundled():
    modules = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))["modules"]
    assert {m["name"] for m in modules["routing"]["metrics"]} >= {"bgpPeerState", "bgpPeerAdminStatus", "ospfNbrState"}
    for name in ("prometheus.yml", "prometheus.yml.tpl"):
        text = (ROOT / "monitoring" / name).read_text(encoding="utf-8")
        assert "job_name: forgesre-snmp-routing" in text and "/api/v1/sd/snmp-routing" in text
    groups = {g["name"]: g for g in yaml.safe_load((ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8"))["groups"]}
    assert {r["alert"] for r in groups["forgesre-routing"]["rules"]} == {"BgpPeerDown", "OspfNeighbourLost"}
