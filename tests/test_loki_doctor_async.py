"""Loki LogQL and System Health probes use httpx.AsyncClient (5s)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from app.api import DOCTOR_HTTP_TIMEOUT, _http, _http_async
from app.metrics import run_sync
from app.services import LOKI_TIMEOUT, _query_loki_selector, _query_loki_selector_async, query_loki_async

ROOT = Path(__file__).resolve().parents[1]


def test_loki_and_doctor_use_async_client_only():
    services = (ROOT / "backend" / "app" / "services.py").read_text(encoding="utf-8")
    api = (ROOT / "backend" / "app" / "api.py").read_text(encoding="utf-8")
    assert "AsyncClient" in services
    assert "httpx.Client(" not in services
    assert "async with httpx.AsyncClient" in api
    assert "httpx.Client(" not in api
    assert LOKI_TIMEOUT == 5.0
    assert DOCTOR_HTTP_TIMEOUT == 5.0


def test_query_loki_selector_async_parses_streams(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        qs = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query))
        assert qs.get("query") == ['{job="syslog",hostname="db-01"}']
        assert qs.get("limit") == ["10"]
        assert qs.get("direction") == ["backward"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {"values": [["1", "line-a"], ["2", "line-b"]]},
                        {"values": [["3", "line-c"]]},
                    ]
                }
            },
        )

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.httpx.AsyncClient", fake_client)
    lines = asyncio.run(
        _query_loki_selector_async('{job="syslog",hostname="db-01"}', 10, None, None)
    )
    assert lines == ["line-a", "line-b", "line-c"]
    assert any("loki/api/v1/query_range" in url for url in seen)
    assert _query_loki_selector('{job="syslog",hostname="db-01"}', 10, None, None) == [
        "line-a",
        "line-b",
        "line-c",
    ]


def test_query_loki_selector_async_timeout_returns_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ReadTimeout("loki blip")

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.httpx.AsyncClient", fake_client)
    assert asyncio.run(_query_loki_selector_async("{job=\"syslog\"}", 5, None, None)) is None
    assert _query_loki_selector("{job=\"syslog\"}", 5, None, None) is None


def test_query_loki_async_hostname_before_ip(monkeypatch):
    order: list[str] = []

    async def fake(query, limit, start, end):
        del limit, start, end
        order.append(query)
        if "hostname" in query:
            return ["from-hostname"]
        return ["from-ip"]

    monkeypatch.setattr("app.services._query_loki_selector_async", fake)
    lines = asyncio.run(
        query_loki_async(
            query='{job="syslog",hostname="db-01"} or {job="syslog",ip="10.1.1.8"}',
            limit=10,
        )
    )
    assert order[0].startswith('{job="syslog",hostname=')
    assert "ip=" in order[1]
    assert lines == ["from-hostname", "from-ip"]


def test_http_async_ok_and_status_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/-/ready"):
            return httpx.Response(200, text="Prometheus is Ready.\n")
        return httpx.Response(503, text="down")

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        assert kwargs.get("timeout") == DOCTOR_HTTP_TIMEOUT
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.api.httpx.AsyncClient", fake_client)
    ok = asyncio.run(_http_async("http://127.0.0.1:9090/-/ready", "GET"))
    assert ok["status"] == "ok"
    err = asyncio.run(_http_async("http://127.0.0.1:9090/-/down", "GET"))
    assert err["status"] == "error"
    assert "503" in (err.get("why") or "")
    wrapped = run_sync(_http_async("http://127.0.0.1:9090/-/ready", "GET"))
    assert wrapped["status"] == "ok"


def test_http_async_network_blip_is_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("connection refused")

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.api.httpx.AsyncClient", fake_client)
    result = asyncio.run(_http_async("http://127.0.0.1:9/-/ready", "GET"))
    assert result["status"] == "error"
    assert "127.0.0.1:9" in (result.get("why") or "")
    assert _http("http://127.0.0.1:9/-/ready", "GET")["status"] == "error"
