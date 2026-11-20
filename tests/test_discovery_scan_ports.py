"""Scan now shows Assets-sized Port cubes for TCP / SNMP / Zabbix and names the source."""

from __future__ import annotations

from pathlib import Path

from app.jobs import DISCOVERY_SCAN_KIND, scan_ports_view
from app.models import Job
from app.seed import seed
from app.db import Base, SessionLocal, engine
from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_scan_ports_view_green_only_for_collect_sources():
    idle = scan_ports_view(None, zabbix_on=False)
    assert idle["status"] == "idle"
    assert idle["line"].startswith("No scan yet")
    assert [cube["label"] for cube in idle["cubes"]] == ["22", "80", "443", "9100", "9182", "SNMP", "10050", "12345"]
    assert all(cube["tone"] == "grey" for cube in idle["cubes"])

    pending = Job(kind=DISCOVERY_SCAN_KIND, status="pending", object_id="scan", payload={"cidrs": ["10.1.1.8/32"]})
    live = scan_ports_view(pending, zabbix_on=True)
    assert live["live"] is True
    assert all(cube["tone"] == "yellow" for cube in live["cubes"])
    assert "pending" in live["line"]

    done = Job(
        kind=DISCOVERY_SCAN_KIND,
        status="done",
        object_id="scan",
        payload={"result": {"found": 1, "skipped": 0, "ports": [22, 9100, 161, 10050], "cidrs": ["10.1.1.8/32"]}},
    )
    view = scan_ports_view(done, zabbix_on=True)
    by_label = {cube["label"]: cube for cube in view["cubes"]}
    assert by_label["22"]["tone"] == "yellow"
    assert by_label["80"]["tone"] == "grey"
    assert by_label["9100"]["tone"] == "green"
    assert by_label["SNMP"]["tone"] == "green"
    assert by_label["10050"]["tone"] == "green"
    assert by_label["12345"]["tone"] == "grey"
    assert "node :9100" in view["line"]
    assert "SNMP UDP/161" in view["line"]
    assert "Zabbix passive :10050" in view["line"]
    off = scan_ports_view(done, zabbix_on=False)
    assert next(cube["tone"] for cube in off["cubes"] if cube["label"] == "10050") == "yellow"
    assert "Zabbix :10050" not in off["line"]


