"""Safer Discovery: suggested /24, confirm cidrs, candidate exporter/SNMP flags."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import run_scan, upsert_candidate
from app.jobs import DISCOVERY_SCAN_KIND, run_pending_jobs
from app.main import app
from app.migrate import migrate
from app.models import DiscoveryCandidate, Job
from app.seed import seed
from app.settings import settings
from discovery import (
    MAX_HOSTS_PER_CIDR,
    MAX_HOSTS_TOTAL,
    normalize_cidrs,
    resolve_scan_cidrs,
    suggested_management_cidr,
)

ROOT = Path(__file__).resolve().parents[1]


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


def test_suggested_management_cidr_is_primary_slash24():
    assert suggested_management_cidr("10.66.1.42") == "10.66.1.0/24"
    assert suggested_management_cidr("127.0.0.1") is None
    assert suggested_management_cidr("169.254.1.1") is None
    assert suggested_management_cidr("not-an-ip") is None
    assert normalize_cidrs("10.1.2.3/24, bogon, 10.1.2.0/24") == ["10.1.2.0/24"]
    assert MAX_HOSTS_PER_CIDR == 256
    assert MAX_HOSTS_TOTAL == 1024
    resolved = resolve_scan_cidrs([], ip_output="2: eth0 inet 10.1.1.5/24")
    assert resolved["cidrs"] == []
    assert resolved["source"] == "none"
    assert resolve_scan_cidrs(["10.9.9.0/28"])["cidrs"] == ["10.9.9.0/28"]


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
    assert row.windows_exporter is False
    assert row.snmp_ok is False
    again = upsert_candidate(
        db,
        "10.77.1.9",
        "Possible network device",
        [161],
        snmp_ok=True,
        node_exporter=False,
        windows_exporter=False,
    )
    db.commit()
    db.refresh(again)
    assert again.snmp_ok is True
    assert again.node_exporter is False
    assert again.open_ports == [161]
    db.close()


def test_run_scan_skips_when_cidrs_empty(monkeypatch):
    db = _db()
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "cidrs", [])
    result = run_scan(db)
    assert result["skipped_reason"] == "empty_cidrs"
    assert result["found"] == 0
    assert result["cidrs"] == []
    db.close()


def test_confirm_and_scan_writes_yaml_and_page_layout(tmp_path, monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
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
    original_yaml = dict(settings.yaml)
    settings.yaml = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    monkeypatch.setattr("discovery.suggested_management_cidr", lambda: "10.55.0.0/24")
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kwargs: {"found": 0, "skipped": 0, "cidrs": cidrs or []},
    )
    try:
        client = _login()
        page = client.get("/discovery")
        assert page.status_code == 200
        assert "discovery-actions" in page.text
        assert "Confirm &amp; scan" in page.text or "Confirm & scan" in page.text
        assert "node_exporter" in page.text
        assert "windows_exporter" in page.text
        assert "SNMP" in page.text
        assert "10.55.0.0/24" in page.text
        assert 'name="cidrs"' in page.text
        assert page.text.find("Scan now") < page.text.find("NetBox sync")

        empty = client.post("/discovery/scan", data={"confirm": "1", "cidrs": ""}, follow_redirects=False)
        assert empty.status_code == 302
        assert "Confirm" in unquote(empty.headers.get("location") or "")

        confirmed = client.post(
            "/discovery/scan",
            data={"confirm": "1", "cidrs": "10.55.0.0/24"},
            follow_redirects=False,
        )
        assert confirmed.status_code == 302
        loc = unquote(confirmed.headers.get("location") or "")
        assert "queued" in loc.lower() or "Confirmed" in loc
        written = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert written["discovery"]["cidrs"] == ["10.55.0.0/24"]
        assert settings.discovery_cidrs == ["10.55.0.0/24"]

        scan_now = client.post("/discovery/scan", data={"cidrs": "10.55.0.0/24"}, follow_redirects=False)
        assert scan_now.status_code == 302
        assert "queued" in unquote(scan_now.headers.get("location") or "").lower()
        db = SessionLocal()
        run_pending_jobs(db)
        db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
        db.commit()
        db.close()
    finally:
        settings.yaml = original_yaml


def test_discovery_template_side_by_side_and_columns():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "discovery-actions" in html
    assert "discovery-actions" in css
    assert "Confirm" in html
    assert "node_exporter" in html
    assert "windows_exporter" in html
    assert "SNMP" in html
    assert html.find("Scan now") < html.find("NetBox sync")
    assert "app.css?v=disc-1" in base
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "primary IPv4" in handbook
    assert "Confirm & scan" in handbook or "Confirm &amp; scan" in handbook
    assert "256" in handbook and "1024" in handbook


def test_candidate_api_exposes_flags():
    db = _db()
    row = upsert_candidate(
        db,
        "10.88.1.2",
        "Possible Windows server",
        [9182],
        windows_exporter=True,
    )
    db.commit()
    client = _login()
    listed = client.get("/api/v1/discovery/candidates")
    assert listed.status_code == 200
    match = next(item for item in listed.json() if item["ip"] == "10.88.1.2")
    assert match["windows_exporter"] is True
    assert match["node_exporter"] is False
    assert match["snmp_ok"] is False
    assert match["open_ports"] == [9182]
    db.close()
