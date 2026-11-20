"""Interface labels from the alert, and the if_mib list when Prometheus has series."""

from __future__ import annotations


from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import format_snmp_uptime, snmp_surface
from app.main import app
from app.models import Asset
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


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
