from discovery import empty_scan_steps, probe_host

from app.inventory import (
    reset_scan_snapshot,
    run_scan,
    scan_snapshot,
    seed_demo_candidate,
)
from app.models import Asset, DiscoveryCandidate
from app.seed import DEMO_ASSET, seed
from app.settings import settings


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client):
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}


def _linux_probe(ip, **kwargs):
    del kwargs
    return {
        "ip": ip,
        "open_ports": [22, 9100],
        "snmp_ok": False,
        "proposed_role": "Possible Linux server",
        "alive": True,
        "exporter_kind": "linux",
        "detect_message": "Detected node_exporter on :9100",
        "steps": [
            {"id": "ssh", "label": "TCP 22", "color": "green", "detail": "SSH open"},
            {"id": "web", "label": "TCP 80/443", "color": "red", "detail": "no TCP 80 or 443"},
            {"id": "node", "label": ":9100", "color": "green", "detail": "TCP node_exporter"},
            {"id": "windows", "label": ":9182", "color": "red", "detail": "no TCP 9182"},
            {"id": "snmp", "label": "SNMP UDP/161", "color": "red", "detail": "no SNMP UDP/161 reply"},
            {
                "id": "metrics",
                "label": "HTTP /metrics",
                "color": "green",
                "detail": "Detected node_exporter on :9100",
            },
        ],
    }


def test_probe_host_steps_match_real_scanner():
    import discovery as discovery_mod

    orig_tcp = discovery_mod._open_tcp
    orig_snmp = discovery_mod.probe_snmp_udp
    discovery_mod._open_tcp = lambda ip, port, timeout: port in {22, 9100}
    discovery_mod.probe_snmp_udp = lambda ip, timeout=0.4: False
    try:
        result = probe_host(
            "10.66.9.10",
            metrics_fetcher=lambda url, timeout: (
                (200, "# HELP node_uname_info\nnode_cpu_seconds_total 1\n", "")
                if ":9100" in url
                else (None, "", "timeout")
            ),
        )
    finally:
        discovery_mod._open_tcp = orig_tcp
        discovery_mod.probe_snmp_udp = orig_snmp
    ids = [item["id"] for item in result["steps"]]
    assert ids == ["ssh", "web", "node", "windows", "snmp", "metrics"]
    by_id = {item["id"]: item for item in result["steps"]}
    assert by_id["ssh"]["color"] == "green"
    assert by_id["web"]["color"] == "red"
    assert by_id["node"]["color"] == "green"
    assert by_id["windows"]["color"] == "red"
    assert by_id["snmp"]["color"] == "red"
    assert by_id["metrics"]["color"] == "green"
    assert "cidr" not in ids
    assert "ping" not in ids
    assert result["alive"] is True
    assert result["exporter_kind"] == "linux"


def test_empty_scan_steps_include_cidr_not_icmp():
    ids = [item["id"] for item in empty_scan_steps()]
    assert ids[0] == "cidr"
    assert "ping" not in ids
    assert "metrics" in ids
    assert "snmp" in ids


def test_run_scan_does_not_auto_approve(monkeypatch):
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "mode", "automatic")
    monkeypatch.setitem(settings.yaml["discovery"], "cidrs", ["10.66.1.8/32"])
    monkeypatch.setattr("discovery.probe_host", _linux_probe)
    reset_scan_snapshot()
    db = _db()
    result = run_scan(db)
    assert result["found"] == 1
    row = db.query(DiscoveryCandidate).filter_by(ip="10.66.1.8").one()
    assert row.status == "new"
    assert db.query(Asset).filter_by(ip="10.66.1.8").first() is None
    snap = scan_snapshot()
    assert snap["status"] == "done"
    assert snap["found"] == 1
    assert snap["auto_approve"] is False
    by_id = {item["id"]: item for item in snap["steps"]}
    assert by_id["cidr"]["color"] == "green"
    assert by_id["ssh"]["color"] == "green"
    assert by_id["metrics"]["color"] == "green"
    host = snap["hosts_by_ip"]["10.66.1.8"]
    assert host["outcome"] == "found"
    db.close()


def test_run_scan_skips_demo_lab_and_does_not_approve(monkeypatch):
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "mode", "automatic")
    monkeypatch.setitem(settings.yaml["discovery"], "cidrs", ["10.10.10.20/32"])
    monkeypatch.setattr("discovery.probe_host", _linux_probe)
    reset_scan_snapshot()
    db = _db()
    result = run_scan(db)
    assert result["found"] == 0
    assert result["skipped"] == 1
    assert result["lab_skipped"] == 1
    extra = db.query(DiscoveryCandidate).filter_by(ip="10.10.10.20").first()
    assert extra is None
    demo = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    assert demo.ip == "10.10.10.20"
    snap = scan_snapshot()
    assert snap["hosts_by_ip"]["10.10.10.20"]["outcome"] == "lab"
    db.close()


def test_seed_demo_candidate_waits_for_approve():
    db = _db()
    row = seed_demo_candidate(db)
    assert row.status == "new"
    assert row.ip == "10.20.30.41"
    assert db.query(Asset).filter_by(ip="10.20.30.41").first() is None
    db.close()


def test_discovery_page_shows_steps_and_approve():
    from fastapi.testclient import TestClient

    from app.main import app

    db = _db()
    seed_demo_candidate(db)
    client = TestClient(app)
    _login(client)
    page = client.get("/discovery")
    assert page.status_code == 200
    text = page.text
    assert "Scan now" in text
    assert "Last scan" in text
    assert "Waiting for Approve" in text
    assert "Approve" in text
    assert "Ignore" in text
    assert "TCP 22" in text
    assert "TCP 80/443" in text
    assert ":9100" in text
    assert ":9182" in text
    assert "SNMP UDP/161" in text
    assert "HTTP /metrics" in text
    assert "not ICMP ping" in text
    assert "10.20.30.41" in text
    assert "NEW DEVICE DETECTED" in text
    snap = client.get("/api/v1/discovery/scan")
    assert snap.status_code == 200
    body = snap.json()
    assert body["status"] in {"idle", "done", "running", "error"}
    assert [item["id"] for item in body["steps"]][0] == "cidr"
    db.close()


def test_approve_from_discovery_page_creates_asset():
    from fastapi.testclient import TestClient

    from app.main import app

    db = _db()
    row = seed_demo_candidate(db)
    client = TestClient(app)
    _login(client)
    posted = client.post(f"/discovery/{row.id}/approve", follow_redirects=False)
    assert posted.status_code in {302, 303}
    db.refresh(row)
    assert row.status == "approved"
    asset = db.query(Asset).filter_by(ip="10.20.30.41").one()
    assert asset.source == "discovery"
    db.close()
