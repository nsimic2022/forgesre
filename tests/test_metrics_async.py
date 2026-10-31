"""Async Prometheus HTTP: AsyncClient, 5s cap, type-aware PromQL."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import uuid4

import httpx

from app.metrics import (
    PROMETHEUS_CONNECT_TIMEOUT,
    PROMETHEUS_TIMEOUT,
    fetch_instant_metric,
    get_asset_load,
    prometheus_timeout,
    query_prometheus_expr,
    query_prometheus_range,
    range_timeout,
    run_sync,
)
from app.services import query_prometheus, query_prometheus_expr as sync_expr
from app.inventory import create_manual_asset
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    n = uuid4().int
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0x7F or 1}"


def test_prometheus_timeout_is_five_seconds():
    bound = prometheus_timeout(30.0)
    assert PROMETHEUS_TIMEOUT == 5.0
    assert PROMETHEUS_CONNECT_TIMEOUT == 2.0
    assert bound.read == 5.0
    assert bound.connect == 2.0
    assert range_timeout(30 * 24 * 3600) == 5.0
    assert 2.0 <= range_timeout(60.0) <= 5.0


def test_metrics_module_uses_async_client_only():
    src = (ROOT / "backend" / "app" / "metrics.py").read_text(encoding="utf-8")
    assert "AsyncClient" in src
    assert "httpx.Client(" not in src
    assert "import requests" not in src
    services = (ROOT / "backend" / "app" / "services.py").read_text(encoding="utf-8")
    assert "httpx.Client(timeout=timeout)" not in services
    assert 'prometheus_url}/api/v1/query' not in services


def test_get_asset_load_uses_node_exporter_for_linux():
    seen: list[str] = []

    def fake(expr: str, timeout: float = 5.0) -> dict:
        del timeout
        seen.append(expr)
        return {"value": 12.0, "query": expr}

    async def _run():
        return await get_asset_load(
            {
                "asset_id": "lnx-load-01",
                "hostname": "lnx-load-01",
                "ip": "10.8.1.10",
                "type": "Linux Server",
                "monitoring_profile": "linux-standard",
                "scrape_address": "10.8.1.10:9100",
            },
            query_fn=fake,
        )

    out = asyncio.run(_run())
    blob = " ".join(seen) + " " + str(out.get("queries") or {})
    assert "node_cpu_seconds_total" in blob
    assert "ifOperStatus" not in blob
    assert "ifHCInOctets" not in blob
    assert out.get("cpu_percent") == 12.0


def test_get_asset_load_uses_snmp_for_network_device():
    seen: list[str] = []

    def fake(expr: str, timeout: float = 5.0) -> dict:
        del timeout
        seen.append(expr)
        return {"value": 3.0, "query": expr}

    async def _run():
        return await get_asset_load(
            {
                "asset_id": "sw-load-01",
                "hostname": "sw-load-01",
                "ip": "10.8.1.20",
                "type": "Network device",
                "monitoring_profile": "network-switch",
                "snmp_port": 161,
            },
            asset_type="Network device",
            query_fn=fake,
        )

    out = asyncio.run(_run())
    blob = " ".join(seen) + " " + str(out.get("queries") or {})
    assert "ifHCInOctets" in blob or "ifOperStatus" in blob
    assert "node_cpu_seconds_total" not in blob
    assert out.get("queries")


def test_get_asset_load_gathers_queries_in_parallel():
    started: list[float] = []

    async def fake(expr: str, timeout: float = 5.0) -> dict:
        del timeout
        started.append(time.monotonic())
        await asyncio.sleep(0.2)
        return {"value": 1.0, "query": expr}

    async def _run():
        t0 = time.monotonic()
        out = await get_asset_load(
            {
                "asset_id": "lnx-par-01",
                "hostname": "lnx-par-01",
                "ip": "10.8.1.30",
                "type": "Linux Server",
                "monitoring_profile": "linux-standard",
                "scrape_address": "10.8.1.30:9100",
            },
            query_fn=fake,
        )
        return time.monotonic() - t0, out

    elapsed, out = asyncio.run(_run())
    assert len(started) >= 3
    assert elapsed < 0.55
    assert out.get("cpu_percent") == 1.0


def test_fetch_instant_returns_empty_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ReadTimeout("slow prometheus")

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=prometheus_timeout(0.2)) as client:
            rows = await fetch_instant_metric("up", timeout=0.2, client=client)
            sample = await query_prometheus_expr("up", timeout=0.2, client=client)
            ranged = await query_prometheus_range("up", start=1.0, end=2.0, timeout=0.2, client=client)
        return rows, sample, ranged

    rows, sample, ranged = asyncio.run(_run())
    assert rows == []
    assert "error" in sample
    assert "error" in ranged


def test_query_prometheus_expr_parses_instant_value():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/query" in str(request.url)
        return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "41.5"]}]}})

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=prometheus_timeout()) as client:
            return await query_prometheus_expr("node_cpu_seconds_total", client=client)

    out = asyncio.run(_run())
    assert out["value"] == 41.5
    assert out["query"] == "node_cpu_seconds_total"


def test_sync_wrappers_still_work_from_services(monkeypatch):
    async def fake_expr(expr: str, timeout: float = 5.0, **kwargs):
        del timeout, kwargs
        return {"value": 7.0, "query": expr}

    monkeypatch.setattr("app.metrics.query_prometheus_expr", fake_expr)
    out = sync_expr("up")
    assert out["value"] == 7.0
    assert run_sync(fake_expr("up"))["value"] == 7.0


def test_query_prometheus_sync_overlay_still_demo(monkeypatch):
    db = _db()
    ip = _ip()
    linux = create_manual_asset(
        db,
        hostname=f"async-prom-{uuid4().hex[:6]}",
        ip=ip,
        type="Linux Server",
        monitoring_profile="linux-standard",
        scrape_address=f"{ip}:9100",
        actor="tester",
    )

    async def fake_load(asset, **kwargs):
        del kwargs
        info_id = getattr(asset, "asset_id", "")
        return {"queries": {"cpu_percent": "node_cpu_seconds_total"}, "cpu_percent": 9.0, "asset": info_id}

    monkeypatch.setattr("app.metrics.get_asset_load", fake_load)
    out = query_prometheus(linux)
    assert out["cpu_percent"] == 9.0
    db.close()
