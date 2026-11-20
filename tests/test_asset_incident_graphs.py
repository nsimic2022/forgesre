"""Asset-page charts, and incident charts that start before the drop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_metrics import incident_graph_bounds
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.models import Asset, Incident
from app.seed import seed
from app.services import graph_window_label, next_incident_number, query_prometheus_range, range_step

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "static" / "app.js"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ts(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


@pytest.fixture(autouse=True)
def _drop_rows():
    yield
    db = _db()
    db.query(Incident).filter(Incident.fingerprint.like("aigraph:%")).delete(synchronize_session=False)
    db.query(Asset).filter(Asset.hostname.like("gr-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _linux(db):
    token = uuid4().hex[:8]
    n = int(token[:4], 16)
    ip = f"10.{140 + (n % 40)}.{(n // 40) % 250}.{1 + (n % 200)}"
    asset = create_manual_asset(
        db,
        hostname=f"gr-{token}",
        ip=ip,
        type="Linux Server",
        actor="tester",
    )
    return asset


def _incident(db, asset: Asset, *, status: str = "OPEN", started: datetime, resolved: datetime | None = None) -> Incident:
    row = Incident(
        number=next_incident_number(db),
        title=f"Graph window {uuid4().hex[:6]}",
        severity="CRITICAL",
        status=status,
        fingerprint=f"aigraph:{uuid4().hex}",
        started_at=started,
        resolved_at=resolved,
        asset_id=asset.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_graph_window_label_is_clocks_only(monkeypatch):
    monkeypatch.setenv("FORGESRE_TIMEZONE", "UTC")
    start = datetime(2026, 10, 9, 11, 15, tzinfo=timezone.utc).timestamp()
    end = datetime(2026, 10, 9, 12, 20, tzinfo=timezone.utc).timestamp()
    assert graph_window_label(start, end) == "11:15–12:20"
    later = datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc).timestamp()
    assert graph_window_label(start, later) == "09.10 11:15–10.10 00:05"
    assert "incident" not in graph_window_label(start, later).lower()


def test_incident_bounds_start_an_hour_before_and_stop_at_resolve():
    started = datetime(2026, 10, 9, 12, 15, tzinfo=timezone.utc)
    now = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
    open_row = Incident(status="OPEN", started_at=started)
    bounds = incident_graph_bounds(open_row, now=now)
    assert bounds["start"] == now.timestamp() - 24 * 3600
    assert bounds["end"] == now.timestamp()
    assert bounds["marker"] == started.timestamp()
    assert bounds["start"] < bounds["marker"] < bounds["end"]

    resolved = started + timedelta(minutes=25)
    done = Incident(status="RESOLVED", started_at=started, resolved_at=resolved)
    closed = incident_graph_bounds(done, now=now)
    assert closed["end"] == resolved.timestamp()
    assert closed["end"] < now.timestamp()
    assert closed["start"] == resolved.timestamp() - 24 * 3600
    assert range_step(closed["end"] - closed["start"]) == "1m"
    assert range_step((now.timestamp() - (now - timedelta(days=2)).timestamp()) + 3600) == "5m"


def test_query_range_sends_the_window_and_keeps_the_drop(monkeypatch):
    seen: dict = {}

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            seen["url"] = url
            seen["params"] = dict(params or {})
            start = float(params["start"])
            return _Resp(
                {
                    "data": {
                        "result": [
                            {"values": [[start, "1"], [start + 50, "1"], [start + 100, "1"]]},
                            {"values": [[start, "40"], [start + 50, "40"], [start + 100, "0"]]},
                        ]
                    }
                }
            )

    monkeypatch.setattr("app.metrics.httpx.AsyncClient", _Client)
    out = query_prometheus_range("node_cpu_seconds_total", start=1_000.0, end=2_000.0, step="5m")
    assert seen["url"].endswith("/api/v1/query_range")
    assert seen["params"]["start"] == 1000.0
    assert seen["params"]["end"] == 2000.0
    assert seen["params"]["step"] == "5m"
    assert out["values"] == [40.0, 40.0, 0.0]
    assert len(out["series"]) == 2
    assert out["times"] == [1000.0, 1050.0, 1100.0]


def test_resolved_incident_graph_ends_when_it_ended(monkeypatch):
    db = _db()
    asset = _linux(db)
    started = datetime.now(timezone.utc) - timedelta(hours=5)
    resolved = started + timedelta(minutes=40)
    row = _incident(db, asset, status="RESOLVED", started=started, resolved=resolved)
    asset_id = asset.asset_id
    number = row.number
    db.close()
    seen: dict = {}

    monkeypatch.setattr(
        "app.services.query_prometheus_expr",
        lambda expr, timeout=5.0: {"value": 1.0, "query": expr},
    )

    def fake_range(expr, hours=1.0, step="5m", timeout=2.0, start=None, end=None):
        del expr, hours, timeout
        seen["start"] = start
        seen["end"] = end
        seen["step"] = step
        return {"values": [1.0, 0.0], "times": [float(start), float(end)]}

    monkeypatch.setattr("app.services.query_prometheus_range", fake_range)
    body = _client().get(f"/api/v1/assets/{asset_id}/metrics?incident={number}").json()
    assert abs(seen["end"] - _ts(resolved)) < 1
    assert seen["end"] < datetime.now(timezone.utc).timestamp() - 3600
    assert seen["start"] < started.timestamp()
    assert body["window"]["marker"] is not None
    assert "–" in body["window"]["label"]
    assert len(body["window"]["label"]) < 40
