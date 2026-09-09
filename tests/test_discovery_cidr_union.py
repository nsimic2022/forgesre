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


def test_discovery_page_and_save_scan(monkeypatch, tmp_path: Path):
    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.migrate import migrate
    from app.seed import seed
    from app.settings import settings

    cfg = tmp_path / "forgesre.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "discovery": {"enabled": True, "mode": "semi-automatic", "cidrs": ["10.1.0.0/28"]},
                "inventory": {"provider": "local", "netbox": {"enabled": False, "mode": "disabled"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "config_path", cfg)
    settings.reload_yaml()
    monkeypatch.setattr(
        "discovery.detect_connected_networks",
        lambda **kwargs: {
            "cidrs": ["10.20.30.0/25"],
            "interfaces": [
                {"iface": "eth0", "addr": "10.20.30.5", "cidr": "10.20.30.0/25", "prefixlen": 25}
            ],
            "skipped": [],
            "warnings": [],
            "host_count": 126,
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kw: {
            "found": 0,
            "skipped": 0,
            "cidrs": ["10.1.0.0/28", "10.20.30.0/25"],
            "yaml": ["10.1.0.0/28"],
            "auto": ["10.20.30.0/25"],
            "source": "yaml+auto",
            "warnings": [],
        },
    )

    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "discovery-actions" in page.text
    assert "10.1.0.0/28" in page.text
    assert "10.20.30.0/25" in page.text
    assert "node_exporter" in page.text
    assert "Save &amp; scan" in page.text or "Save & scan" in page.text
    assert "hardcoded /24" in page.text.lower() or "not a hardcoded /24" in page.text.lower()

    saved = client.post(
        "/discovery/scan",
        data={"confirm": "1", "cidrs": "10.1.0.0/28,10.2.0.0/28"},
        follow_redirects=False,
    )
    assert saved.status_code == 302
    settings.reload_yaml()
    assert "10.2.0.0/28" in settings.discovery_cidrs
    db.close()


def test_docs_and_template_say_union():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert ("unions" in html.lower()) or ("union" in html.lower()) or ("∪" in html)
    assert "node_exporter" in html
    assert "discovery-actions" in html
    assert "hardcoded /24" in html.lower() or "not a hardcoded /24" in html.lower()
    assert ("union" in handbook.lower()) or ("∪" in handbook) or ("auto-detect" in handbook.lower())
    assert "256" in handbook and "1024" in handbook
