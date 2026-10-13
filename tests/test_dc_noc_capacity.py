"""30-day capacity: the query window is 30 days, and one sample is not a forecast."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import CAPACITY_SECONDS, capacity_panel, forecast_days_to_full
from app.inventory import create_manual_asset
from app.main import app
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_forecast_needs_two_climbing_points():
    assert forecast_days_to_full([1.0], [40.0]) is None
    assert forecast_days_to_full([0.0, 86400.0], [40.0, 40.0]) is None
    assert forecast_days_to_full([0.0, 30 * 86400.0], [50.0, 80.0]) == 20


def test_query_window_is_30_days_and_one_point_is_not_a_forecast():
    db = _db()
    token = uuid4().hex[:8]
    raw = uuid4().int
    asset = create_manual_asset(
        db,
        hostname=f"cap-{token}",
        asset_id=f"cap-{token}",
        ip=f"10.83.{raw % 200 + 1}.{(raw >> 8) % 200 + 1}",
        type="Linux Server",
        actor="tester",
    )
    calls: list[float] = []

    def fetch(expr: str, start: float, end: float) -> dict:
        calls.append(end - start)
        if "filesystem" in expr or "disk" in expr:
            return {"values": [40.0], "times": [start], "query": expr}
        return {"values": [1000.0, 1100.0], "times": [start, end], "query": expr}

    panel = capacity_panel(asset, range_fn=fetch, now=1_700_000_000.0)
    assert panel["window_seconds"] == CAPACITY_SECONDS
    assert calls and all(span == CAPACITY_SECONDS for span in calls)
    disk = next(row for row in panel["rows"] if row["label"] == "Disk")
    assert disk["points"] == 1
    assert disk["forecast_days"] is None
    assert disk["state"] == "thin"
    assert "Not enough history" in disk["note"]
    assert "days to full" not in disk["note"]

    def climbing(expr: str, start: float, end: float) -> dict:
        if "filesystem" in expr or "disk" in expr:
            return {"values": [50.0, 80.0], "times": [start, end], "query": expr}
        return {"error": "absent", "values": []}

    rich = capacity_panel(asset, range_fn=climbing, now=1_700_000_000.0)
    disk = next(row for row in rich["rows"] if row["label"] == "Disk")
    assert disk["forecast_days"] == 20
    assert disk["note"] == "~20 days to full"
    assert disk["spark"]
    net = next(row for row in rich["rows"] if row["label"] == "Interface receive")
    assert net["state"] == "not_scraped"
    assert net["forecast_days"] is None

    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    page = client.get(f"/assets/{asset.asset_id}")
    assert page.status_code == 200
    assert 'data-capacity' in page.text
    assert 'data-capacity-window="' + str(CAPACITY_SECONDS) in page.text or f'data-capacity-window="{CAPACITY_SECONDS:.1f}"' in page.text or "Not scraped." in page.text
    db.close()
