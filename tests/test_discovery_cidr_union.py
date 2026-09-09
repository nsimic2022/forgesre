"""Discovery CIDRs = YAML discovery.cidrs ∪ auto-detected connected nets."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from discovery import (
    MAX_HOSTS_PER_CIDR,
    MAX_HOSTS_TOTAL,
    detect_connected_networks,
    merge_cidrs,
    normalize_cidrs,
    resolve_scan_cidrs,
    scan_plan,
)

ROOT = Path(__file__).resolve().parents[1]

SAMPLE_IP = """\
1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 10.20.30.5/25 brd 10.20.30.127 scope global eth0
3: eth1    inet 192.168.10.8/24 brd 192.168.10.255 scope global eth1
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
5: br-abc123    inet 172.18.0.1/16 scope global br-abc123
6: eth2    inet 169.254.99.1/16 scope link eth2
"""


def test_detect_uses_real_prefix_not_hardcoded_slash24():
    detected = detect_connected_networks(ip_output=SAMPLE_IP)
    assert detected["cidrs"] == ["10.20.30.0/25", "192.168.10.0/24"]
    assert "10.20.30.0/24" not in detected["cidrs"]
    reasons = {row["iface"]: row["reason"] for row in detected["skipped"]}
    assert reasons["docker0"] == "docker_bridge"
    assert reasons["lo"] == "loopback"


def test_resolve_scan_cidrs_union_yaml_and_auto():
    both = resolve_scan_cidrs(["10.9.9.0/28"], ip_output=SAMPLE_IP)
    assert both["source"] == "yaml+auto"
    assert both["yaml"] == ["10.9.9.0/28"]
    assert both["auto"] == ["10.20.30.0/25", "192.168.10.0/24"]
    assert both["cidrs"] == ["10.9.9.0/28", "10.20.30.0/25", "192.168.10.0/24"]

    yaml_only = resolve_scan_cidrs(["10.1.1.0/30"], ip_output="")
    assert yaml_only["source"] == "yaml"
    assert yaml_only["cidrs"] == ["10.1.1.0/30"]

    auto_only = resolve_scan_cidrs([], ip_output=SAMPLE_IP)
    assert auto_only["source"] == "auto"
    assert auto_only["cidrs"] == ["10.20.30.0/25", "192.168.10.0/24"]

    none = resolve_scan_cidrs([], ip_output="")
    assert none["source"] == "none"
    assert none["cidrs"] == []


def test_merge_and_limits():
    assert merge_cidrs(["10.0.0.0/30", "10.0.0.0/30"], ["10.0.0.0/29"]) == [
        "10.0.0.0/30",
        "10.0.0.0/29",
    ]
    assert normalize_cidrs(["0.0.0.0/0", "169.254.0.0/16", "10.1.2.0/28"]) == ["10.1.2.0/28"]
    plan = scan_plan(["10.0.0.0/16"])
    assert plan["total"] == MAX_HOSTS_PER_CIDR == 256
    assert MAX_HOSTS_TOTAL == 1024
    assert plan["truncated"] is True


def test_run_scan_merges_and_skips_assets(monkeypatch):
    from app.db import Base, SessionLocal, engine
    from app.inventory import create_manual_asset, run_scan
    from app.migrate import migrate
    from app.models import DiscoveryCandidate
    from app.seed import seed
    from app.settings import settings

    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    create_manual_asset(db, hostname="already-there", ip="10.20.30.5", actor="t")
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "cidrs", ["10.99.0.0/30"])
    monkeypatch.setattr(
        "discovery.detect_connected_networks",
        lambda **kwargs: {
            "cidrs": ["10.20.30.0/29"],
            "interfaces": [],
            "skipped": [],
            "warnings": [],
            "host_count": 6,
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "discovery.probe_host",
        lambda ip, **kwargs: {
            "ip": ip,
            "open_ports": [22, 9100] if ip.endswith(".1") else [161],
            "snmp_ok": not ip.endswith(".1"),
            "proposed_role": "Possible Linux server" if ip.endswith(".1") else "Possible network device",
            "alive": True,
            "exporter_kind": "linux" if ip.endswith(".1") else "network",
            "detect_message": "",
        },
    )
    result = run_scan(db)
    assert result["source"] == "yaml+auto"
    assert "10.99.0.0/30" in result["cidrs"]
    assert "10.20.30.0/29" in result["cidrs"]
    assert result["skipped"] >= 1
    row = db.query(DiscoveryCandidate).filter_by(ip="10.20.30.1").first()
    assert row is not None
    assert row.node_exporter is True
    assert row.status == "new"
    db.close()


def test_run_scan_yaml_only_when_auto_empty(monkeypatch):
    from app.db import Base, SessionLocal, engine
    from app.inventory import run_scan
    from app.migrate import migrate
    from app.seed import seed
    from app.settings import settings

    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "cidrs", ["10.55.0.0/30"])
    monkeypatch.setattr(
        "discovery.detect_connected_networks",
        lambda **kwargs: {
            "cidrs": [],
            "interfaces": [],
            "skipped": [],
            "warnings": [],
            "host_count": 0,
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "discovery.probe_host",
        lambda ip, **kwargs: {
            "ip": ip,
            "open_ports": [],
            "snmp_ok": False,
            "proposed_role": "No open ports",
            "alive": False,
            "exporter_kind": "",
            "detect_message": "",
        },
    )
    result = run_scan(db)
    assert result["source"] == "yaml"
    assert result["cidrs"] == ["10.55.0.0/30"]
    assert result["auto"] == []
    db.close()


def test_auto_env_off_skips_live_ifaces(monkeypatch):
    monkeypatch.setenv("FORGESRE_DISCOVERY_AUTO", "0")
    detected = detect_connected_networks()
    assert detected["cidrs"] == []
    assert detected["interfaces"] == []


def test_ioctl_fallback_skips_loopback_and_docker(monkeypatch):
    import ipaddress

    monkeypatch.setenv("FORGESRE_DISCOVERY_AUTO", "1")
    monkeypatch.setattr("discovery._ip_cmd_output", lambda: "")
    detected = detect_connected_networks()
    reasons = {row["iface"]: row["reason"] for row in detected["skipped"]}
    if "lo" in reasons:
        assert reasons["lo"] == "loopback"
    if "docker0" in reasons:
        assert reasons["docker0"] == "docker_bridge"
    for cidr in detected["cidrs"]:
        net = ipaddress.ip_network(cidr)
        assert not net.is_loopback
        assert str(net) != "0.0.0.0/0"
    for row in detected["interfaces"]:
        assert 1 <= int(row["prefixlen"]) <= 32
        assert not str(row["cidr"]).endswith("/0")


def test_core_image_has_iproute2():
    text = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "iproute2" in text
    assert "iputils-ping" in text


def test_docs_and_template_say_union():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    blob = html.lower()
    assert "discovery-actions" in html and "node_exporter" in html
    assert ("unions" in blob) or ("∪" in html) or ("auto-detected" in blob and "discovery.cidrs" in blob)
    assert ("hardcoded /24" in blob) or ("real prefix" in blob)
    hb = handbook.lower()
    assert ("union" in hb) or ("auto-detect" in hb) or ("connected" in hb)
    assert "256" in handbook and "1024" in handbook



