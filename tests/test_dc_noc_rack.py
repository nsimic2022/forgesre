"""Row and Rack are text extras with the same kind of filter as Site and VLAN."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import assets_matching, create_manual_asset
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


def _host(db, token: str, rack: str, dc_row: str):
    raw = uuid4().int
    return create_manual_asset(
        db,
        hostname=f"rk-{token}",
        asset_id=f"rk-{token}",
        ip=f"10.84.{raw % 200 + 1}.{(raw >> 8) % 200 + 1}",
        type="Linux Server",
        extras={"row": dc_row, "rack": rack},
        owner_email="rack@dc.local",
        actor="tester",
    )


def test_filter_by_rack_returns_that_host():
    db = _db()
    token = uuid4().hex[:8]
    here = _host(db, token, "R14", "A")
    other = _host(db, token + "b", "R15", "B")
    rows = db.query(Asset).all()
    assert {item.asset_id for item in assets_matching(rows, rack="R14")} == {here.asset_id}
    assert {item.asset_id for item in assets_matching(rows, rack="r14", row="A")} == {here.asset_id}
    assert assets_matching(rows, rack="R14", row="B") == []
    client = _client()
    page = client.get("/assets?q=R14")
    assert page.status_code == 200
    body = page.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert here.asset_id in body
    assert other.asset_id not in body
    filters = page.text.split("assets-filters", 1)[1].split("</form>", 1)[0]
    assert 'data-asset-filter="' not in filters
    assert "<select" not in filters
    assert 'name="extra_row"' in client.get("/discovery").text
    assert 'name="extra_rack"' in client.get("/discovery").text

    opened = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": f"RackProbe{token}", "severity": "warning", "asset": here.asset_id},
                    "annotations": {"summary": "rack probe"},
                }
            ],
        },
    )
    number = opened[0].number
    inc = client.get("/incidents?rack=R14").text
    assert number in inc
    assert 'data-incident-filter="rack"' in inc
    other_inc = client.get("/incidents?rack=R15").text
    assert number not in other_inc
    db.close()
