"""SNMP and syslog flows against a fake Prometheus / snmp_exporter / Alloy / Loki, and the per-device
(``target``) checks of the flow engine."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from app.db import Base, SessionLocal, engine
from app.models import Asset, Job
from app.settings import settings
from tests.test_incident_tools import _client

SNMP_YML = "auths:\n  public_v2:\n    version: 2\n  # forgesre-asset-auths begin\n  # forgesre-asset-auths end\nmodules: {}\n"


class Stack:
    def __init__(self, asset_id: str):
        self.asset_id = asset_id
        self.walk_error = ""

    def answer(self, path: str, query: dict) -> tuple[int, str, object]:
        if path == "/api/v1/targets":
            return 200, "json", {"data": {"activeTargets": [
                {"health": "down", "scrapePool": "forgesre-snmp", "labels": {"asset": self.asset_id}, "lastError": "request timeout"},
                {"health": "up", "scrapePool": "forgesre-snmp", "labels": {"asset": "other-sw"}},
                {"health": "up", "scrapePool": "node", "labels": {"instance": "db-01"}}]}}
        if path == "/snmp":
            if self.walk_error:
                return 500, "text", f"An error has occurred while serving metrics:\n\n{self.walk_error}"
            return 200, "text", "# HELP ifInOctets x\nifInOctets{ifIndex=\"1\"} 5\nifOutOctets{ifIndex=\"1\"} 7\nsnmp_scrape_walk_duration_seconds 0.1\n"
        if path == "/metrics":  # snmp_exporter and Alloy share this fake
            return 200, "text", ('loki_source_syslog_entries_total{component_id="loki.source.syslog.devices"} 120\n'
                                 'loki_source_syslog_parsing_errors_total{component_id="x"} 3\n'
                                 'loki_write_sent_entries_total{host="127.0.0.1:3100"} 117\n')
        if path == "/ready":
            return 200, "text", "ready"
        if path.startswith("/loki/api/v1/label/"):
            values = ["sw-flow-01", "stranger-fw"] if path.endswith("/hostname/values") else ["10.9.9.9"]
            return 200, "json", {"status": "success", "data": values}
        if path == "/loki/api/v1/query":
            expr = (query.get("query") or [""])[0]
            value = "0" if 'ip="' in expr else "42"
            return 200, "json", {"data": {"result": [{"value": [0, value]}]}}
        return 404, "text", "no"


@pytest.fixture
def stack(monkeypatch, tmp_path: Path):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(Job).filter(Job.kind.in_(["flow_check", "flow_ai"])).delete(synchronize_session=False)
    tag = uuid4().hex[:6]
    asset = Asset(asset_id=f"sw-flow-{tag}", hostname="sw-flow-01", ip="127.0.0.1", type="Switch", monitoring_profile="network-switch")
    db.add(asset)
    db.commit()
    (tmp_path / "snmp.yml").write_text(SNMP_YML, encoding="utf-8")
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr(type(settings), "snmp_enabled", property(lambda s: True))
    fake = Stack(asset.asset_id)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            code, kind, body = fake.answer(parts.path, parse_qs(parts.query))
            data = (json.dumps(body) if kind == "json" else str(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if kind == "json" else "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    for attr in ("app.flow_prometheus.prom_url", "app.flow_snmp.exporter_url", "app.flow_syslog.loki_url", "app.flow_syslog.alloy_url"):
        monkeypatch.setattr(attr, lambda url=url: url)
    monkeypatch.setattr("app.api.syslog_listen_ports", lambda port=514: {"udp": True, "tcp": False})
    yield db, fake, asset
    server.shutdown()
    db.delete(db.get(Asset, asset.id))
    db.commit()
    db.close()


def _steps(result):
    return {s["id"]: s for s in result["lanes"][0]["steps"]}


def test_snmp_whole_flow(stack):
    from app.flow_snmp import check

    db, _, _ = stack
    s = _steps(check(db))
    assert s["inventory"]["status"] == "ok" and s["auths"]["status"] in {"ok", "warn"}
    assert s["exp_tcp"]["status"] == "ok" and s["exporter"]["status"] == "ok"
    assert s["targets"]["status"] == "warn" and "1 failing" in s["targets"]["detail"]
    assert s["targets"]["rows"][0]["error"] == "request timeout"


def test_snmp_one_device_never_shows_the_community(stack, monkeypatch):
    from app import snmp_get
    from app.flow_snmp import check_device

    db, fake, asset = stack
    seen = {}
    monkeypatch.setattr(snmp_get, "system_info", lambda ip, community, **k: seen.update(c=community) or {"sysDescr": "Cisco IOS 15.2"})
    monkeypatch.setenv("SNMP_COMMUNITY", "s3cr3t-community")
    s = _steps(check_device(db, asset.asset_id))
    assert s["asset"]["status"] == "ok" and s["auth"]["status"] == "ok"
    assert s["get"]["status"] == "ok" and "Cisco IOS" in s["get"]["detail"] and seen["c"] == "s3cr3t-community"
    assert s["walk"]["status"] == "ok" and "2 series" in s["walk"]["detail"]
    assert s["target"]["status"] == "bad" and "request timeout" in s["target"]["detail"]
    fake.walk_error = "error walking target 127.0.0.1: request timeout (#1)"
    s = _steps(check_device(db, str(asset.id)))
    assert s["walk"]["status"] == "bad" and "request timeout" in s["walk"]["detail"]
    assert "s3cr3t" not in json.dumps(s)


def test_syslog_whole_flow_and_one_device(stack):
    from app.flow_syslog import alloy_counters, check, check_device

    db, _, asset = stack
    assert alloy_counters("loki_write_dropped_entries_total 2\n")["dropped"] == 2.0
    s = _steps(check(db))
    assert s["listen"]["status"] == "warn" and "TCP/514" in s["listen"]["detail"]
    assert s["alloy"]["status"] == "warn" and "3 could not be parsed" in s["alloy"]["detail"]
    assert s["loki"]["status"] == "ok"
    assert s["senders"]["status"] == "warn" and "1 not in the inventory" in s["senders"]["detail"]
    assert s["read"]["status"] == "ok" and "42" in s["read"]["detail"]
    one = _steps(check_device(db, asset.asset_id))
    assert one["by_host"]["status"] == "ok" and one["by_ip"]["status"] == "warn"


def test_per_device_checks_run_as_jobs_and_are_not_journaled(stack, monkeypatch):
    from app import snmp_get
    from app.flows import job_key, notice_changes
    from app.jobs import run_pending_jobs

    db, _, asset = stack
    monkeypatch.setattr(snmp_get, "system_info", lambda *a, **k: {"error": "no SNMP answer"})
    admin = _client("admin")
    assert admin.post("/api/v1/integrations/snmp/flow?target=bad!name").status_code == 404
    assert admin.post("/api/v1/integrations/zabbix/flow?target=x").status_code == 404  # zabbix has no per-device check
    assert job_key("snmp", asset.asset_id) == f"snmp@{asset.asset_id}"
    job = admin.post(f"/api/v1/integrations/snmp/flow?target={asset.asset_id}").json()["job"]
    for _ in range(10):
        run_pending_jobs(db, limit=10)
        db.expire_all()
        if db.get(Job, job).status in {"done", "failed"}:
            break
    data = admin.get(f"/api/v1/integrations/snmp/flow?target={asset.asset_id}").json()
    assert data["status"] == "done" and data["target_label"] == "sw-flow-01 (127.0.0.1)" and data["watch"] is False
    assert _steps(data["result"])["get"]["status"] == "bad"
    assert notice_changes(db, f"snmp@{asset.asset_id}", None, data["result"]) == []
    whole = admin.get("/api/v1/integrations/snmp/flow").json()
    assert "target" not in whole and whole["targets"] is True


def test_stack_cards_link_to_their_flow():
    admin = _client("admin")
    cards = admin.get("/api/v1/integrations").json()
    links = {c["id"]: c.get("flow") for group in cards.values() for c in group}
    assert links["snmp"] == "/settings/integrations/snmp"
    assert links["alloy"] == links["loki"] == "/settings/integrations/syslog"
    assert links["alertmanager"] == "/settings/integrations/prometheus"
