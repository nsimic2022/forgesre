"""HOST-RESOURCES / UCD host families: Collecting cube, second SNMP job, source labels."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_extras import extras_from_form, normalize_extras
from app.asset_metrics import asset_metric_panel
from app.asset_types import snmp_port_for
from app.asset_verify import verify_target
from app.db import Base, SessionLocal, engine
from app.inventory import is_snmp_asset, sd_snmp_host_targets, sd_snmp_targets
from app.main import app
from app.models import Asset
from app.seed import seed
from app.snmp_families import (
    ALL_FAMILIES,
    families_from_form,
    host_metrics_on,
    http_source_label,
    snmp_metrics_on,
    snmp_source_label,
    sources_from_form,
    tile_source_label,
)
from rca.collector import promql_queries_for

ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"Authorization": "Bearer forgesre-dev-webhook-token"}


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for asset in db.query(Asset).filter(Asset.asset_id.like("shm-%")).all():
        db.delete(asset)
    db.commit()
    db.close()


def _ip() -> str:
    raw = uuid4().int
    return f"10.{180 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def _post(client: TestClient, asset_id: str, type_: str, ip: str | None = None, **extra):
    data = {"asset_id": asset_id, "hostname": asset_id, "ip": ip or _ip(), "type": type_, **extra}
    return client.post("/assets", data=data, follow_redirects=False)


def test_families_default_poll_all_family_snmp_net_only():
    linux_poll = {
        "asset_id": "shm-lnx",
        "type": "Linux Server",
        "ip": "10.1.1.1",
        "extras": {"snmp_poll": "1"},
    }
    switch = {"asset_id": "shm-sw", "type": "Switch", "ip": "10.1.1.2"}
    linux_http = {"asset_id": "shm-http", "type": "Linux Server", "ip": "10.1.1.3", "scrape_address": "10.1.1.3:9100"}
    assert snmp_port_for(linux_poll) == 161
    assert snmp_metrics_on(linux_poll) == frozenset(ALL_FAMILIES)
    assert host_metrics_on(linux_poll) is True
    assert snmp_metrics_on(switch) == frozenset({"net"})
    assert host_metrics_on(switch) is False
    assert snmp_metrics_on(linux_http) == frozenset()
    assert host_metrics_on(linux_http) is False
    assert snmp_metrics_on({"type": "Switch", "ip": "10.1.1.4", "extras": {"snmp_metrics": "cpu,disk"}}) == frozenset(
        {"cpu", "disk"}
    )
    assert families_from_form("") is None
    assert families_from_form("1") == ""
    assert families_from_form("1", net="1", cpu="1", mem="on") == "net,cpu,mem"
    stored = normalize_extras({"snmp_poll": "1", "snmp_metrics": "cpu,mem"})
    assert stored["snmp_poll"] == "1"
    assert stored["snmp_metrics"] == "cpu,mem"
    assert extras_from_form("1", snmp_poll="1")["snmp_poll"] == "1"
    assert "snmp_metrics" not in extras_from_form("1", snmp_poll="1")
    assert extras_from_form("1", snmp_metrics="cpu")["snmp_metrics"] == "cpu"
    assert sources_from_form("") is None
    assert sources_from_form("1") == {"prometheus": False, "snmp": False}
    assert sources_from_form("1", prometheus="1", snmp="on") == {"prometheus": True, "snmp": True}


def test_source_labels_name_node_9100_vs_snmp():
    linux = {"type": "Linux Server", "scrape_address": "10.9.9.9:9100"}
    linux_poll = {**linux, "ip": "10.9.9.9", "extras": {"snmp_poll": "1"}}
    windows = {"type": "Windows Server", "scrape_address": "10.9.9.8:9182"}
    assert http_source_label(linux) == "node :9100"
    assert http_source_label(windows) == "windows :9182"
    assert snmp_source_label(linux) == "SNMP off"
    assert snmp_source_label(linux_poll) == "SNMP UDP/161 · if_mib + host"
    assert tile_source_label(linux, False) == "node :9100"
    assert tile_source_label(linux_poll, True) == "SNMP UDP/161"
    assert "not SNMP" in http_source_label({"type": "Linux Server"})


def test_host_sd_includes_poll_linux_not_plain_switch():
    db = _db()
    lnx_ip, sw_ip, skip_ip = _ip(), _ip(), _ip()
    db.add_all(
        [
            Asset(
                asset_id="shm-lnx-sd",
                hostname="shm-lnx-sd",
                ip=lnx_ip,
                type="Linux Server",
                scrape_address=f"{lnx_ip}:9100",
                extras={"snmp_poll": "1"},
            ),
            Asset(
                asset_id="shm-sw-sd",
                hostname="shm-sw-sd",
                ip=sw_ip,
                type="Switch",
                scrape_address="",
            ),
            Asset(
                asset_id="shm-lnx-http",
                hostname="shm-lnx-http",
                ip=skip_ip,
                type="Linux Server",
                scrape_address=f"{skip_ip}:9100",
            ),
        ]
    )
    db.commit()
    assert is_snmp_asset(db.query(Asset).filter_by(asset_id="shm-lnx-sd").one()) is True
    if_mib = {row["labels"]["asset"]: row for row in sd_snmp_targets(db)}
    host = {row["labels"]["asset"]: row for row in sd_snmp_host_targets(db)}
    assert if_mib["shm-lnx-sd"]["targets"] == [lnx_ip]
    assert if_mib["shm-sw-sd"]["targets"] == [sw_ip]
    assert "shm-lnx-http" not in if_mib
    assert host["shm-lnx-sd"]["labels"]["snmp_module"] == "host"
    assert host["shm-lnx-sd"]["targets"] == [lnx_ip]
    assert "shm-sw-sd" not in host
    assert "shm-lnx-http" not in host
    db.close()


def test_snmp_host_http_sd_requires_token():
    db = _db()
    ip = _ip()
    db.add(
        Asset(
            asset_id="shm-sd-api",
            hostname="shm-sd-api",
            ip=ip,
            type="Linux Server",
            extras={"snmp_poll": "1"},
        )
    )
    db.commit()
    client = TestClient(app)
    assert client.get("/api/v1/sd/snmp-host").status_code == 401
    body = client.get("/api/v1/sd/snmp-host", headers=TOKEN).json()
    assert any(item["targets"] == [ip] and item["labels"]["snmp_module"] == "host" for item in body)
    db.close()


def test_graphs_use_prometheus_when_scrape_exists_even_if_snmp_poll_is_on():
    from app.asset_metrics import safe_asset_metric_panel
    from app.snmp_families import metric_family_heading, metric_source_cubes

    linux_http = {
        "asset_id": "shm-http-graph",
        "type": "Linux Server",
        "ip": "10.8.8.8",
        "scrape_address": "10.8.8.8:9100",
        "monitoring_profile": "linux-standard",
    }
    linux_poll = {
        **linux_http,
        "asset_id": "shm-poll-graph",
        "extras": {"snmp_poll": "1"},
    }

    def query(expr: str) -> dict:
        return {"value": 1.0 if "up{" in expr or "hrProcessorLoad" in expr or "ifHC" in expr else 12.0, "query": expr}

    http_panel = asset_metric_panel(linux_http, query_fn=query, range_fn=lambda expr: {"values": []})
    poll_panel = asset_metric_panel(linux_poll, query_fn=query, range_fn=lambda expr: {"values": []})
    http_cpu = next(tile for tile in http_panel["tiles"] if tile["key"] == "cpu_percent")
    poll_cpu = next(tile for tile in poll_panel["tiles"] if tile["key"] == "cpu_percent")
    assert http_cpu["source"] == "node :9100"
    assert "node_cpu_seconds_total" in http_cpu["query"]
    assert "forgesre-snmp-host" not in http_cpu["query"]
    assert {tile["key"] for tile in poll_panel["tiles"]} == {
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "network",
    }
    assert poll_cpu["source"] == "node :9100"
    assert "node_cpu_seconds_total" in poll_cpu["query"]
    assert "forgesre-snmp-host" not in poll_cpu["query"]
    assert poll_panel["sources"]["http"] == "node :9100"
    assert poll_panel["sources"]["snmp"].startswith("SNMP UDP/161")
    assert metric_family_heading(linux_poll) == "Prometheus metrics"
    assert {row["key"]: row["on"] for row in metric_source_cubes(linux_poll)} == {
        "prometheus": True,
        "zabbix": False,
        "snmp": True,
        "glpi": False,
    }
    packed = promql_queries_for(linux_poll)
    assert "node_cpu_seconds_total" in packed["cpu_percent"][0]
    snmp_only = {**linux_poll, "scrape_address": ""}
    assert "forgesre-snmp-host" in promql_queries_for(snmp_only)["cpu_percent"][0]
    assert metric_family_heading(snmp_only) == "SNMP metrics"
    switch = promql_queries_for({"asset_id": "shm-sw-q", "type": "Switch", "ip": "10.8.8.1"})
    assert "cpu_percent" not in switch
    assert "ifHCInOctets" in switch["net_rx"][0]

    win = {
        "asset_id": "shm-win-graph",
        "hostname": "shm-win-graph",
        "type": "Windows Server",
        "ip": "10.8.8.7",
        "scrape_address": "10.8.8.7:9182",
        "monitoring_profile": "windows-standard",
        "snmp_port": 161,
    }
    win_packed = promql_queries_for(win)
    assert "windows_cpu_time_total" in win_packed["cpu_percent"][0]
    assert "forgesre-snmp-host" not in win_packed["cpu_percent"][0]
    assert metric_family_heading(win) == "Prometheus metrics"
    assert {row["key"]: row["on"] for row in metric_source_cubes(win)}["snmp"] is True

    def empty_http(expr: str) -> dict:
        if "hrProcessorLoad" in expr or "ifHC" in expr or 'job="forgesre-snmp"' in expr:
            return {"value": 7.0, "query": expr}
        return {"value": None, "query": expr}

    fallback = safe_asset_metric_panel(linux_poll, query_fn=empty_http, range_fn=lambda expr: {"values": []})
    fb_cpu = next(tile for tile in fallback["tiles"] if tile["key"] == "cpu_percent")
    assert fb_cpu["value"] == 7.0
    assert "hrProcessorLoad" in fb_cpu["query"]


def test_linux_poll_verify_passes_on_snmp_when_9100_is_down():
    item = {
        "asset_id": "shm-lnx-verify",
        "hostname": "shm-lnx-verify",
        "ip": "10.223.9.86",
        "type": "Linux Server",
        "scrape_address": "10.223.9.86:9100",
        "monitoring_profile": "linux-standard",
        "extras": {"snmp_poll": "1"},
    }

    def fetch(url, timeout):
        del timeout
        return None, "", "connection refused"

    report = verify_target(
        item,
        ping_runner=lambda host, timeout: (0, "rtt min/avg/max = 1.0/1.0/1.0 ms", ""),
        metrics_fetcher=fetch,
        snmp_prober=lambda host, timeout=0.4, port=161: True,
        in_http_sd=True,
        in_snmp_sd=True,
        query_fn=lambda expr: {"value": 1.0, "query": expr},
    )
    assert report.vclass == "linux"
    assert report.port.ok is True
    assert "SNMP UDP" in (report.port.detail or "")
    assert report.family.ok is True
    assert "SNMP UDP" in report.family.detail
    assert "node :9100" in report.family.detail
    assert report.prom.ok is True
    assert 'job="forgesre-snmp"' in (report.prom.detail or report.prom.name or "") or report.prom.ok is True
    assert report.series.ok is True
    assert "SNMP" in report.series.detail
    assert report.overall == "PASS"
    extra_text = " ".join(item.detail for item in report.extra)
    assert ":9100" in extra_text


def test_verify_mentions_systemonly_when_sysdescr_ok_but_no_series():
    item = {
        "asset_id": "shm-view-block",
        "hostname": "shm-view-block",
        "ip": "10.223.9.86",
        "type": "Linux Server",
        "scrape_address": "",
        "monitoring_profile": "linux-standard",
        "extras": {"snmp_poll": "1"},
    }

    def fetch(url, timeout):
        del url, timeout
        return None, "", "connection refused"

    report = verify_target(
        item,
        ping_runner=lambda host, timeout: (0, "rtt min/avg/max = 1.0/1.0/1.0 ms", ""),
        metrics_fetcher=fetch,
        snmp_prober=lambda host, timeout=0.4, port=161: True,
        in_http_sd=False,
        in_snmp_sd=True,
        query_fn=lambda expr: {"value": None, "query": expr},
    )
    assert "sysDescr" in (report.series.detail or "") or "systemonly" in (report.series.detail or "")
    assert "view" in (report.series.detail or "").lower()


def test_forgesre_cli_puts_agents_on_pythonpath():
    text = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert 'PYTHONPATH="$ROOT/backend:$ROOT/agents"' in text
    assert text.count('PYTHONPATH="$ROOT/backend:$ROOT/agents"') >= 2
    assert 'PYTHONPATH="$ROOT/backend"' not in text.replace('PYTHONPATH="$ROOT/backend:$ROOT/agents"', "")


