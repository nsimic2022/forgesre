"""Scan now shows Assets-sized Port cubes for TCP / SNMP / Zabbix and names the source."""

from __future__ import annotations

from pathlib import Path

from app.jobs import DISCOVERY_SCAN_KIND, enqueue_discovery_scan, run_pending_jobs, scan_ports_view
from app.models import Job
from app.seed import seed
from app.db import Base, SessionLocal, engine
from discovery import SCAN_CUBE_META, SCAN_PORT_ORDER, ZABBIX_AGENT_PORT
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


def test_scan_cube_labels_match_assets_port_size():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    reach = css.split(".reach-sq {", 1)[1].split("}", 1)[0]
    assert "min-width: 2.7rem" in reach
    assert "height: 1.15rem" in reach
    assert "font-size: 0.68rem" in reach
    scan = css.split(".scan-port-cubes {", 1)[1].split("}", 1)[0]
    assert "flex" in scan
    cards = css.split(".discovery-actions .card {", 1)[1].split("}", 1)[0]
    assert "min-height: 12.5rem" in cards
    assert "min-width: 2.7rem" not in scan
    assert [item[0] for item in SCAN_CUBE_META] == list(SCAN_PORT_ORDER)
    assert ZABBIX_AGENT_PORT == 10050
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    assert '_discovery_scan_ports.html' in html
    frag = (ROOT / "frontend" / "templates" / "_discovery_scan_ports.html").read_text(encoding="utf-8")
    assert 'class="reach-sq' in frag
    assert "scan-port-cubes" in frag
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-58" in base and "app.js?v=v09-R" in base


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


def test_discovery_page_renders_scan_cubes_and_stores_result(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kwargs: {
            "found": 1,
            "skipped": 0,
            "cidrs": cidrs or [],
            "ports": [22, 80, 443, 9100],
        },
    )
    job = enqueue_discovery_scan(db, actor="t", cidrs=["10.9.9.9/32"], saved=True)
    run_pending_jobs(db)
    db.refresh(job)
    assert job.status == "done"
    assert (job.payload or {}).get("result", {}).get("ports") == [22, 80, 443, 9100]
    db.close()
    page = _login().get("/discovery")
    assert page.status_code == 200
    assert 'id="scan-ports"' in page.text
    assert "scan-port-cubes" in page.text
    assert 'class="reach-sq green"' in page.text
    assert ">9100<" in page.text
    assert ">SNMP<" in page.text
    assert ">10050<" in page.text
    assert ">12345<" in page.text
    assert "Collecting from node :9100" in page.text
    frag = _login().get("/discovery/scan-ports")
    assert frag.status_code == 200
    assert "scan-port-cubes" in frag.text
