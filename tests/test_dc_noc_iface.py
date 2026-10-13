"""Interface labels from the alert, and the if_mib list when Prometheus has series."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import format_snmp_uptime, interface_facts, snmp_surface
from app.inventory import create_manual_asset
from app.main import app
from app.models import Asset
from app.seed import seed
from app.services import ingest_alertmanager


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_fixture_ifname_appears_on_the_incident():
    db = _db()
    token = uuid4().hex[:8]
    raw = uuid4().int
    asset = create_manual_asset(
        db,
        hostname=f"sw-{token}",
        asset_id=f"sw-{token}",
        ip=f"10.79.{raw % 200 + 1}.{(raw >> 8) % 200 + 1}",
        type="Switch",
        owner_email="noc@dc.local",
        actor="tester",
    )
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "NetworkInterfaceDown",
                        "severity": "warning",
                        "asset": asset.asset_id,
                        "instance": asset.asset_id,
                        "ifName": "Gi0/1",
                        "ifIndex": "7",
                        "ifAlias": "uplink-core",
                    },
                    "annotations": {"summary": "Interface down", "description": "Gi0/1"},
                }
            ],
        },
    )
    assert len(created) == 1
    facts = interface_facts(created[0])
    assert facts["ifName"] == "Gi0/1"
    assert facts["ifIndex"] == "7"
    assert facts["ifAlias"] == "uplink-core"
    client = _client()
    page = client.get(f"/incidents/{created[0].number}")
    assert page.status_code == 200
    assert 'data-if-name' in page.text and "Gi0/1" in page.text
    assert 'data-if-index' in page.text and ">7<" in page.text
    assert 'data-if-alias' in page.text and "uplink-core" in page.text
    listed = client.get("/incidents")
    assert "Gi0/1" in listed.text and "data-iface" in listed.text
    db.close()


def test_snmp_surface_uses_series_and_does_not_invent_rows():
    asset = Asset(asset_id="sw-lab", hostname="sw-lab", type="Switch", ip="10.1.1.1")

    def vector(expr: str) -> dict:
        if expr.startswith("ifOperStatus"):
            return {
                "series": [
                    {
                        "labels": {"ifName": "Gi0/2", "ifIndex": "2", "ifAlias": "server-a"},
                        "value": 2,
                    }
                ]
            }
        return {"series": [{"labels": {}, "value": 100 * 3600 * 3}]}

    view = snmp_surface(asset, vector_fn=vector)
    assert view["state"] == "ok"
    assert view["interfaces"][0]["ifName"] == "Gi0/2"
    assert view["interfaces"][0]["ifIndex"] == "2"
    assert view["interfaces"][0]["ifAlias"] == "server-a"
    assert view["interfaces"][0]["state"] == "down"
    assert view["uptime"] == "3h 0m"
    assert format_snmp_uptime(100 * 86400) == "1d 0h"

    def empty(_expr: str) -> dict:
        return {"error": "down", "series": []}

    missing = snmp_surface(asset, vector_fn=empty)
    assert missing["state"] == "not_scraped"
    assert missing["interfaces"] == []
    assert missing["note"] == "Not scraped."
    linux = Asset(asset_id="lin", hostname="lin", type="Linux Server", ip="10.1.1.2")
    assert snmp_surface(linux, vector_fn=vector)["show"] is False
