"""Read-only MCP server at /mcp: off by default, bearer token, Origin check, read-only tools."""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, utcnow
from app.settings import Settings, settings

H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}


def _rpc(client, method, params=None, headers=None, rid=1):
    return client.post("/mcp/", headers={**H, **(headers or {})}, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def _on(monkeypatch, enabled=True, tok="mcp-secret"):
    old = settings.yaml
    settings.yaml = {**(old or {}), "mcp": {"enabled": enabled}, "system": {"public_url": "https://forgesre.dc.local"}}
    monkeypatch.setattr(Settings, "_secret", lambda self, key: tok if key == "MCP_TOKEN" else "")
    return old


def test_off_by_default_and_needs_the_token(monkeypatch):
    old = _on(monkeypatch, enabled=False)
    try:
        with TestClient(create_app()) as client:
            assert _rpc(client, "tools/list").status_code == 404
            settings.yaml = {**settings.yaml, "mcp": {"enabled": True}}
            assert _rpc(client, "tools/list").status_code == 401
            assert _rpc(client, "tools/list", headers={"Authorization": "Bearer wrong"}).status_code == 401
            evil = _rpc(client, "tools/list", headers={"Authorization": "Bearer mcp-secret", "Origin": "https://evil.example"})
            assert evil.status_code == 403
    finally:
        settings.yaml = old


def test_tools_are_read_only_and_answer(monkeypatch):
    old = _on(monkeypatch)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    sw = Asset(asset_id=f"mcp-sw-{tag}", hostname=f"mcp-sw-{tag}", ip="10.12.0.1", type="Switch", owner_email="secret@dc.local")
    db.add(sw)
    db.flush()
    db.add(Asset(asset_id=f"mcp-db-{tag}", hostname=f"mcp-db-{tag}", ip="10.12.0.2", type="Oracle DB", parent_id=sw.id))
    db.add(Incident(number=f"INC-M{tag}", title="Disk 92%", severity="HIGH", status="OPEN", fingerprint=f"mcp:{tag}",
                    asset_id=sw.id, started_at=utcnow()))
    db.commit()
    db.close()
    auth = {"Authorization": "Bearer mcp-secret"}
    try:
        with TestClient(create_app()) as client:
            init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, auth)
            assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "forgesre"
            tools = _rpc(client, "tools/list", headers=auth).json()["result"]["tools"]
            names = {t["name"] for t in tools}
            assert names == {"noc_overview", "list_incidents", "get_incident", "search_assets", "asset_dependencies", "on_call_now", "slo_status", "checks_status",
                             "incident_metric_names", "incident_metric", "incident_logs", "incident_interfaces", "similar_incidents", "search_vendor_docs"}
            assert all(t["annotations"]["readOnlyHint"] is True for t in tools)

            def call(name, args=None):
                r = _rpc(client, "tools/call", {"name": name, "arguments": args or {}}, auth).json()["result"]
                assert r["isError"] is False, r
                return json.loads(r["content"][0]["text"])

            inc = call("get_incident", {"number": f"INC-M{tag}"})
            assert inc["title"] == "Disk 92%" and inc["asset"]["hostname"] == f"mcp-sw-{tag}"
            assert "secret@dc.local" not in json.dumps(inc) and "evidence" not in inc
            assert any(r["number"] == f"INC-M{tag}" for r in call("list_incidents", {"status": "active", "limit": 100})["incidents"])
            deps = call("asset_dependencies", {"asset": f"mcp-sw-{tag}"})
            assert deps["behind"] == [f"mcp-db-{tag}"]
            assert "counts" in call("noc_overview") and "slos" in call("slo_status")
            assert call("get_incident", {"number": "INC-NOPE"}) == {"error": "no incident INC-NOPE"}
            # investigator tools: same reads as ForgeAI's tool investigation; a missing source is an answer too
            assert "metrics" in call("incident_metric_names", {"number": f"INC-M{tag}"})
            assert call("incident_metric", {"number": f"INC-M{tag}", "name": "nope"})["source"] == "metric"
            assert call("similar_incidents", {"number": f"INC-M{tag}"})["result"]
            assert call("incident_logs", {"number": "INC-NOPE"}) == {"error": "no incident INC-NOPE"}
            assert call("search_vendor_docs", {"query": "raid"}) == {"hits": []}
    finally:
        settings.yaml = old
