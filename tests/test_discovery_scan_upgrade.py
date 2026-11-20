"""Safer Discovery: suggested /24, confirm cidrs, candidate exporter/SNMP flags."""

from __future__ import annotations

from pathlib import Path

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
