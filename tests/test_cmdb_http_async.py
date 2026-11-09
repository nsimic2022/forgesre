"""Discovery / doctor / Verify HTTP uses AsyncClient + run_sync. CLI urllib stays."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.asset_verify import am_health, am_health_async, prom_query, prom_targets, urllib_am_health
from app.exporter_detect import ExporterDetect, detect_exporter
from app.host_resources import fetch_node_exporter, fetch_node_exporter_async

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"


def test_cmdb_clients_use_async_client_only():
    for name in ("netbox.py", "zabbix.py", "glpi.py"):
        src = (APP / name).read_text(encoding="utf-8")
        assert "httpx.AsyncClient" in src, name
        assert "httpx.Client(" not in src, name
        assert "from app.metrics import run_sync" in src, name


def test_host_resources_node_exporter_is_async_client():
    src = (APP / "host_resources.py").read_text(encoding="utf-8")
    assert "httpx.AsyncClient" in src
    assert "urllib.request" not in src
    assert "timeout: float = 0.25" in src
    assert "fetch_node_exporter_async" in src


def test_asset_verify_keeps_urllib_for_cli_and_async_for_ui():
    src = (APP / "asset_verify.py").read_text(encoding="utf-8")
    assert "def urllib_prom_query(" in src
    assert "def urllib_prom_targets(" in src
    assert "def urllib_am_health(" in src
    assert "urllib.request.urlopen" in src
    assert "async def prom_query_async(" in src
    assert "async def prom_targets_async(" in src
    assert "async def am_health_async(" in src
    api = (APP / "api.py").read_text(encoding="utf-8")
    assert "am_health as urllib_am_health" in api
    assert "prom_targets as urllib_prom_targets" in api
    cli = (APP / "cli_ops.py").read_text(encoding="utf-8")
    assert "urllib_am_health" in cli
    assert "urllib_prom_query" in cli
    assert "urllib_prom_targets" in cli
    assert urllib_am_health.__doc__


def test_exporter_detect_sync_urllib_is_pytest_helper_only():
    src = (APP / "exporter_detect.py").read_text(encoding="utf-8")
    assert "def fetch_metrics(" in src
    assert "urllib.request.urlopen" in src
    body = src.split("def detect_exporter(", 1)[1].split("async def _async_snmp", 1)[0]
    assert "return run_sync(" in body
    assert "asyncio.get_running_loop()" not in body


def test_detect_exporter_without_fetcher_uses_async_path(monkeypatch):
    called: dict[str, str] = {}

    async def fake_type(ip, **kwargs):
        del kwargs
        called["ip"] = ip
        return ExporterDetect(kind="linux", asset_type="Linux Server", message="ok")

    monkeypatch.setattr("app.exporter_detect.detect_exporter_type", fake_type)
    result = detect_exporter("10.1.1.8")
    assert called["ip"] == "10.1.1.8"
    assert result.kind == "linux"


def test_fetch_node_exporter_async_reads_metrics(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/metrics")
        return httpx.Response(200, text="node_memory_MemTotal_bytes 100\n")

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        assert kwargs.get("timeout") == 0.25
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.host_resources.httpx.AsyncClient", fake_client)
    text = asyncio.run(fetch_node_exporter_async("http://127.0.0.1:9100/metrics", timeout=0.25))
    assert "node_memory_MemTotal_bytes" in (text or "")
    assert fetch_node_exporter("http://127.0.0.1:9100/metrics", timeout=0.25)


def test_fetch_node_exporter_timeout_is_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ReadTimeout("node blip")

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.host_resources.httpx.AsyncClient", fake_client)
    assert asyncio.run(fetch_node_exporter_async()) is None
    assert fetch_node_exporter() is None


def test_prom_targets_and_am_health_async(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if path.endswith("/api/v1/targets"):
            return httpx.Response(
                200,
                json={"data": {"activeTargets": [{"scrapeUrl": "http://10.1.1.1:9100/metrics"}]}},
            )
        if "/api/v1/query" in path:
            return httpx.Response(
                200,
                json={"data": {"result": [{"value": [0, "1.5"]}]}},
            )
        if path.endswith("/-/ready"):
            return httpx.Response(200, text="OK")
        return httpx.Response(404, text="no")

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.asset_verify.httpx.AsyncClient", fake_client)
    rows = prom_targets("http://127.0.0.1:9090")
    assert rows["targets"][0]["scrapeUrl"].endswith(":9100/metrics")
    sample = prom_query("up", "http://127.0.0.1:9090")
    assert sample["value"] == 1.5
    health = am_health("http://127.0.0.1:9093")
    assert health["ok"] is True
    assert "/-/ready" in health["detail"]
    assert asyncio.run(am_health_async("http://127.0.0.1:9093"))["ok"] is True
