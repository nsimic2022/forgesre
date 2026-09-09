"""Discovery multi-CIDR autodetection (real prefixes) + candidate flags."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import run_scan, upsert_candidate
from app.main import app
from app.migrate import migrate
from app.seed import seed
from app.settings import settings
from discovery import (
    MAX_HOSTS_PER_CIDR,
    MAX_HOSTS_TOTAL,
    detect_connected_networks,
    normalize_cidrs,
    resolve_scan_cidrs,
    suggested_connected_cidrs,
)

ROOT = Path(__file__).resolve().parents[1]

SAMPLE_IP = """\
1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 10.20.30.5/25 brd 10.20.30.127 scope global eth0
3: eth1    inet 192.168.10.8/24 brd 192.168.10.255 scope global eth1
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
"""


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    return client


def test_detect_uses_real_prefix_not_hardcoded_slash24():
    detected = detect_connected_networks(ip_output=SAMPLE_IP)
    assert detected["cidrs"] == ["10.20.30.0/25", "192.168.10.0/24"]
    assert "10.20.30.0/24" not in detected["cidrs"]
    assert suggested_connected_cidrs(ip_output=SAMPLE_IP) == detected["cidrs"]
    assert normalize_cidrs("10.1.2.3/25, bogon, 0.0.0.0/0") == ["10.1.2.0/25"]
    resolved = resolve_scan_cidrs([], ip_output=SAMPLE_IP)
    assert resolved["source"] == "auto"
    assert resolved["cidrs"] == detected["cidrs"]
    assert MAX_HOSTS_PER_CIDR == 256
    assert MAX_HOSTS_TOTAL == 1024


def test_upsert_candidate_keeps_snmp_and_exporter_flags():
    db = _db()
    row = upsert_candidate(
        db,
        "10.77.1.9",
        "Possible Linux server",
        [22, 9100],
        snmp_ok=False,
        node_exporter=True,
        windows_exporter=False,
    )
    db.commit()
    db.refresh(row)
    assert row.node_exporter is True
    assert row.snmp_ok is False
    db.close()


def test_run_scan_skips_when_no_cidrs_and_no_detect(monkeypatch):
    db = _db()
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "cidrs", [])
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
    result = run_scan(db)
    assert result["skipped_reason"] == "empty_cidrs"
    db.close()


def test_scan_now_saves_and_layout(tmp_path, monkeypatch):
    db = _db()
    db.close()
    cfg = tmp_path / "forgesre.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "discovery": {"enabled": True, "mode": "semi-automatic", "cidrs": []},
                "inventory": {"provider": "local", "netbox": {"enabled": False, "mode": "disabled"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "config_path", cfg)
    settings.yaml = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        "discovery.detect_connected_networks",
        lambda **kwargs: {
            "cidrs": ["10.55.0.0/25", "192.168.7.0/24"],
            "interfaces": [
                {"iface": "eth0", "addr": "10.55.0.10", "cidr": "10.55.0.0/25", "prefixlen": 25},
                {"iface": "eth1", "addr": "192.168.7.2", "cidr": "192.168.7.0/24", "prefixlen": 24},
            ],
            "skipped": [],
            "warnings": [],
            "host_count": 380,
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, merge_auto=True, ip_output=None: {
            "found": 0,
            "skipped": 0,
            "cidrs": cidrs or ["10.55.0.0/25", "192.168.7.0/24"],
            "warnings": [],
            "source": "auto",
        },
    )

    client = _login()
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "discovery-actions" in page.text
    assert "Confirm &amp; scan" not in page.text
    assert "node_exporter" in page.text
    assert "SNMP" in page.text
    assert "10.55.0.0/25" in page.text
    assert "hardcoded /24" in page.text.lower()
    assert page.text.find("Scan now") < page.text.find("NetBox sync")

    scanned = client.post(
        "/discovery/scan",
        data={"cidrs": "10.55.0.0/25, 192.168.7.0/24"},
        follow_redirects=False,
    )
    assert scanned.status_code == 302
    written = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert written["discovery"]["cidrs"] == ["10.55.0.0/25", "192.168.7.0/24"]


def test_discovery_template_side_by_side_and_columns():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "discovery-actions" in html
    assert "discovery-actions" in css
    assert "Confirm &amp; scan" not in html
    assert "node_exporter" in html
    assert "SNMP" in html
    assert "hardcoded /24" in html.lower()
    assert html.find("Scan now") < html.find("NetBox sync")
    assert "app.css?v=disc-1" in base
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "connected" in handbook.lower()
    assert "256" in handbook and "1024" in handbook
    assert "hardcoded" in handbook.lower()


def test_candidate_api_exposes_flags():
    db = _db()
    upsert_candidate(db, "10.88.1.2", "Possible Windows server", [9182], windows_exporter=True)
    db.commit()
    client = _login()
    listed = client.get("/api/v1/discovery/candidates")
    assert listed.status_code == 200
    match = next(item for item in listed.json() if item["ip"] == "10.88.1.2")
    assert match["windows_exporter"] is True
    assert match["node_exporter"] is False
    assert match["snmp_ok"] is False
    db.close()
