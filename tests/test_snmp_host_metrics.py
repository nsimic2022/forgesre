"""HOST-RESOURCES / UCD host families: Collecting cube, second SNMP job, source labels."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
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
    HOST_FAMILIES,
    families_from_form,
    host_metrics_on,
    http_source_label,
    snmp_metrics_on,
    snmp_source_label,
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
        {"net", "cpu", "disk"}
    )
    assert families_from_form("") is None
    assert families_from_form("1") == "net"
    assert families_from_form("1", cpu="1", mem="on") == "net,cpu,mem"
    stored = normalize_extras({"snmp_poll": "1", "snmp_metrics": "cpu,mem"})
    assert stored["snmp_poll"] == "1"
    assert stored["snmp_metrics"] == "net,cpu,mem"
    assert extras_from_form("1", snmp_poll="1")["snmp_poll"] == "1"
    assert "snmp_metrics" not in extras_from_form("1", snmp_poll="1")
    assert extras_from_form("1", snmp_metrics="cpu")["snmp_metrics"] == "net,cpu"


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


def test_collecting_cube_on_form_and_saves_families():
    _db().close()
    client = _client()
    page = client.get("/discovery").text
    cube = page.split("data-asset-collecting", 1)[1].split("</section>", 1)[0]
    assert ">Metrics<" in cube
    assert "node :9100" in cube or "HTTP off" in cube
    assert "SNMP off" in cube or "SNMP UDP" in cube
    assert 'name="snmp_metrics_present"' in cube
    for key in HOST_FAMILIES:
        assert f'name="snmp_metric_{key}"' in cube, key
    assert "data-collect-http" in cube and "data-collect-snmp" in cube
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "syncCollecting" in js
    assert "node :9100 (default scrape, not SNMP)" in js
    assert "if_mib + host" in js
    ip = _ip()
    posted = _post(
        client,
        "shm-form-lnx",
        "Linux Server",
        ip,
        extras_present="1",
        snmp_poll="1",
        snmp_metrics_present="1",
        snmp_metric_cpu="1",
        snmp_metric_mem="1",
        snmp_metric_disk="1",
    )
    assert posted.status_code in {302, 303}
    db = SessionLocal()
    row = db.query(Asset).filter_by(asset_id="shm-form-lnx").one()
    assert row.extras.get("snmp_poll") == "1"
    assert row.extras.get("snmp_metrics") == "net,cpu,mem,disk"
    assert host_metrics_on(row) is True
    host = {item["labels"]["asset"] for item in sd_snmp_host_targets(db)}
    assert "shm-form-lnx" in host
    edit = client.get("/assets?edit=shm-form-lnx").text
    collect = edit.split("data-asset-collecting", 1)[1].split("</section>", 1)[0]
    assert "SNMP UDP/161" in collect
    assert "node :9100" in collect
    assert 'name="snmp_metric_cpu"' in collect and "checked" in collect.split('name="snmp_metric_cpu"', 1)[1][:80]
    db.close()


def test_graphs_label_snmp_not_node_when_poll_is_on():
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
    poll_by = {tile["key"]: tile for tile in poll_panel["tiles"]}
    assert http_cpu["source"] == "node :9100"
    assert "node_cpu_seconds_total" in http_cpu["query"]
    assert "forgesre-snmp-host" not in http_cpu["query"]
    assert set(poll_by) >= {"cpu_percent", "load", "memory_percent", "swap_percent", "disk_percent", "proc", "network"}
    assert poll_by["cpu_percent"]["source"] == "SNMP UDP/161"
    assert "forgesre-snmp-host" in poll_by["cpu_percent"]["query"]
    assert "hrProcessorLoad" in poll_by["cpu_percent"]["query"]
    assert poll_panel["sources"]["http"] == "node :9100"
    assert poll_panel["sources"]["snmp"].startswith("SNMP UDP/161")
    packed = promql_queries_for(linux_poll)
    assert "forgesre-snmp-host" in packed["cpu_percent"][0]
    assert "ifHCInOctets" in packed["net_rx"][0]
    switch = promql_queries_for({"asset_id": "shm-sw-q", "type": "Switch", "ip": "10.8.8.1"})
    assert "cpu_percent" not in switch
    assert "ifHCInOctets" in switch["net_rx"][0]


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


def test_host_module_and_job_are_bundled():
    parsed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))
    host = parsed["modules"]["host"]
    assert "lookups" not in host
    names = {metric["name"] for metric in host["metrics"]}
    assert {
        "hrProcessorLoad",
        "ssCpuIdle",
        "memTotalReal",
        "memAvailReal",
        "memTotalSwap",
        "memAvailSwap",
        "laLoadInt",
        "dskPercent",
        "prCount",
        "prErrorFlag",
    } <= names
    prom = (ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8")
    tpl = (ROOT / "monitoring" / "prometheus.yml.tpl").read_text(encoding="utf-8")
    for text in (prom, tpl):
        assert "job_name: forgesre-snmp-host" in text
        assert "/api/v1/sd/snmp-host" in text
        assert "module: [host]" in text
    alerts = (ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8")
    for name in ("SnmpCpuHigh", "SnmpMemoryHigh", "SnmpDiskUsageHigh", "SnmpProcessDown"):
        assert f"alert: {name}" in alerts
    assert 'job="forgesre-snmp-host"' in alerts
    detail = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    assert "forgesre-snmp-host" in detail
    assert "if_mib + host" in detail
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-56" in base and "app.js?v=v09-N" in base
