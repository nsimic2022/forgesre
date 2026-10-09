"""Asset-page charts, and incident charts that start before the drop."""

from __future__ import annotations

import re
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
    assert bounds["start"] == started.timestamp() - 3600
    assert bounds["end"] == now.timestamp()
    assert bounds["marker"] == started.timestamp()
    assert bounds["start"] < bounds["marker"] < bounds["end"]

    resolved = started + timedelta(minutes=25)
    done = Incident(status="RESOLVED", started_at=started, resolved_at=resolved)
    closed = incident_graph_bounds(done, now=now)
    assert closed["end"] == resolved.timestamp()
    assert closed["end"] < now.timestamp()
    assert range_step(closed["end"] - closed["start"]) == "5m"
    assert range_step((now.timestamp() - (now - timedelta(days=2)).timestamp()) + 3600) == "30m"


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

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
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

    monkeypatch.setattr("app.services.httpx.Client", _Client)
    out = query_prometheus_range("node_cpu_seconds_total", start=1_000.0, end=2_000.0, step="5m")
    assert seen["url"].endswith("/api/v1/query_range")
    assert seen["params"]["start"] == 1000.0
    assert seen["params"]["end"] == 2000.0
    assert seen["params"]["step"] == "5m"
    assert out["values"] == [40.0, 40.0, 0.0]
    assert out["times"] == [1000.0, 1050.0, 1100.0]


def test_asset_detail_graphs_use_the_host_metrics_api(monkeypatch):
    db = _db()
    asset = _linux(db)
    asset_id = asset.asset_id
    db.close()

    def fake_query(expr: str, timeout: float = 5.0) -> dict:
        del timeout
        if "up{" in expr:
            return {"value": 1.0, "query": expr}
        if "node_cpu" in expr:
            return {"value": 22.0, "query": expr}
        return {"value": None, "query": expr}

    def fake_range(*args, **kwargs):
        expr = args[0] if args else kwargs.get("expr", "")
        if "node_cpu" in expr:
            return {"values": [10.0, 22.0, 18.0], "times": [1, 2, 3], "query": expr}
        if "up{" in expr:
            return {"values": [1.0, 1.0, 1.0], "times": [1, 2, 3], "query": expr}
        return {"values": [], "query": expr}

    monkeypatch.setattr("app.services.query_prometheus_expr", fake_query)
    monkeypatch.setattr("app.services.query_prometheus_range", fake_range)

    client = _client()
    page = client.get(f"/assets/{asset_id}")
    assert page.status_code == 200
    text = page.text
    card = text.split("data-asset-metrics", 1)[1].split("</aside>", 1)[0]
    assert f'data-asset="{asset_id}"' in card
    assert "data-asset-graphs" in card
    assert "data-dash-graph-list" in card
    assert "data-dash-graph-empty" in card
    assert "data-dash-graph-caption" in card
    assert "<p" not in card

    api = client.get(f"/api/v1/assets/{asset_id}/metrics")
    assert api.status_code == 200
    body = api.json()
    assert body["asset_id"] == asset_id
    cpu = next(tile for tile in body["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["series"] == [10.0, 22.0, 18.0]
    assert body["window"]["marker"] is None
    assert re.fullmatch(r"\d{2}:\d{2}–\d{2}:\d{2}|\d{2}\.\d{2} \d{2}:\d{2}–\d{2}\.\d{2} \d{2}:\d{2}", body["window"]["label"])

    js = JS.read_text(encoding="utf-8")
    asset_js = js.split("(function bindAssetGraphs() {", 1)[1].split("\n})();", 1)[0]
    assert '"/api/v1/assets/" + encodeURIComponent(asset) + "/metrics"' in asset_js
    assert "?incident=" not in asset_js
    assert "Not scraped." in js and "No samples yet." in js
    assert "dash-chart-marker" in js


def test_incident_graph_query_starts_before_the_incident(monkeypatch):
    db = _db()
    asset = _linux(db)
    other = _linux(db)
    started = datetime.now(timezone.utc) - timedelta(hours=3)
    row = _incident(db, asset, started=started)
    stranger = _incident(db, other, started=started - timedelta(hours=5))
    asset_id = asset.asset_id
    number = row.number
    other_number = stranger.number
    started_ts = _ts(row.started_at)
    db.close()

    calls: list[dict] = []

    def fake_query(expr: str, timeout: float = 5.0) -> dict:
        del timeout
        if "up{" in expr:
            return {"value": 0.0, "query": expr}
        return {"value": None, "query": expr}

    def fake_range(expr, hours=1.0, step="5m", timeout=2.0, start=None, end=None):
        del hours, timeout
        calls.append({"expr": expr, "start": start, "end": end, "step": step})
        start_f = float(start)
        end_f = float(end)
        mid = (start_f + end_f) / 2.0
        if "node_cpu" in expr:
            return {"values": [30.0, 28.0, 0.0], "times": [start_f, mid, end_f], "query": expr}
        if "up{" in expr:
            return {"values": [1.0, 1.0, 0.0], "times": [start_f, mid, end_f], "query": expr}
        return {"values": [10.0, 10.0, 99.0], "times": [start_f, mid, end_f], "query": expr}

    monkeypatch.setattr("app.services.query_prometheus_expr", fake_query)
    monkeypatch.setattr("app.services.query_prometheus_range", fake_range)
    client = _client()

    body = client.get(f"/api/v1/assets/{asset_id}/metrics?incident={number}").json()
    assert calls, "incident chart did not query a range"
    assert all(call["start"] < started_ts - 3500 for call in calls)
    assert all(call["start"] > started_ts - 3700 for call in calls)
    assert all(abs(call["end"] - datetime.now(timezone.utc).timestamp()) < 5 for call in calls)
    assert calls[0]["step"] == "5m"
    assert abs(body["window"]["marker"] - started_ts) < 1
    assert body["window"]["start"] < body["window"]["marker"] <= body["window"]["end"]
    cpu = next(tile for tile in body["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["series"][0] > 0 and cpu["series"][-1] == 0.0
    assert abs(cpu["times"][0] - calls[0]["start"]) < 0.01
    disk = next(tile for tile in body["tiles"] if tile["key"] == "disk_percent")
    assert disk["series"][-1] == 99.0

    detail = client.get(f"/incidents/{number}").text
    assert "data-incident-graphs" in detail
    assert f'data-incident="{number}"' in detail
    assert "data-dash-graph-caption" in detail
    assert "dash-chart-marker" not in detail

    calls.clear()
    plain = client.get(f"/api/v1/assets/{asset_id}/metrics").json()
    assert plain["window"]["marker"] is None
    assert abs(calls[0]["start"] - (datetime.now(timezone.utc).timestamp() - 3600)) < 5

    calls.clear()
    ignored = client.get(f"/api/v1/assets/{asset_id}/metrics?incident={other_number}").json()
    assert ignored["window"]["marker"] is None
    assert abs(calls[0]["start"] - (datetime.now(timezone.utc).timestamp() - 3600)) < 5


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
