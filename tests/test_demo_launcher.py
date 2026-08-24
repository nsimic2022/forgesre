from fastapi.testclient import TestClient

from app.cli_view import format_board, format_detail
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Notification
from app.seed import DEMO_ASSET, seed
from app.services import close_open_incidents, is_demo_incident, run_demo, run_demo_host, run_demo_rca


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client():
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    return client


def _close_demo_fires(db):
    close_open_incidents(db, f"HighCPU:{DEMO_ASSET}", include_resolved=True)
    close_open_incidents(db, f"FilesystemUsageHigh:{DEMO_ASSET}", include_resolved=True)
    close_open_incidents(db, f"NodeExporterDown:{DEMO_ASSET}", include_resolved=True)


def test_dashboard_has_one_run_demo_control_not_two_forms():
    _db().close()
    client = _client()
    home = client.get("/")
    assert home.status_code == 200
    html = home.text
    assert html.count('id="demo-open"') == 1
    assert html.count("data-demo-open") == 1
    assert html.count("Run demo") >= 1
    assert "First-hour walkthrough" not in html
    assert "Run demo workflow" not in html
    assert "Run RCA demo" not in html
    panel_at = html.find('id="demo-panel"')
    assert panel_at > 0
    assert html.find("Linux HighCPU", panel_at) > panel_at
    assert html.find("Linux disk / ForgeRCA", panel_at) > panel_at
    assert html.find("Linux host unreachable", panel_at) > panel_at
    assert html.find('action="/demo"', panel_at) > panel_at
    assert html.find('action="/demo-rca"', panel_at) > panel_at
    assert html.find('action="/demo-reset"', panel_at) > panel_at
    before = html[:panel_at]
    assert 'action="/demo"' not in before
    assert 'action="/demo-rca"' not in before
    infra = html.find(">Infrastructure<")
    assert infra > 0
    assert panel_at < infra


def test_demo_incident_is_marked_demo_in_list_detail_and_api():
    db = _db()
    incident = run_demo(db)
    assert incident is not None
    assert is_demo_incident(incident) is True
    note = (
        db.query(Notification)
        .filter_by(incident_id=incident.id, step_key="immediate")
        .first()
    )
    assert note is not None
    assert note.subject.startswith("[DEMO]")
    assert "DEMO incident on forge-demo-01" in (note.body or "")
    number = incident.number
    _close_demo_fires(db)
    db.close()

    client = _client()
    listing = client.get("/incidents")
    assert listing.status_code == 200
    assert 'class="pill demo"' in listing.text
    assert "DEMO" in listing.text
    assert number in listing.text

    detail = client.get(f"/incidents/{number}")
    assert detail.status_code == 200
    assert 'class="pill demo"' in detail.text
    assert "[DEMO]" in detail.text

    history = client.get("/history")
    assert history.status_code == 200
    assert 'class="pill demo"' in history.text

    esc = client.get("/escalation")
    assert esc.status_code == 200
    assert 'class="pill demo"' in esc.text or "[DEMO]" in esc.text

    dash = client.get("/")
    assert 'class="pill demo"' in dash.text

    api = client.get(f"/api/v1/incidents/{number}")
    assert api.status_code == 200
    body = api.json()
    assert body["demo"] is True
    assert body.get("asset", {}).get("asset_id") == DEMO_ASSET


def test_demo_host_and_rca_are_demo_tagged():
    db = _db()
    disk = run_demo_rca(db)
    host = run_demo_host(db)
    assert disk is not None and host is not None
    assert disk.number != host.number
    assert is_demo_incident(disk) is True
    assert is_demo_incident(host) is True
    assert "FilesystemUsageHigh" in (disk.fingerprint or "")
    assert "NodeExporterDown" in (host.fingerprint or "")
    _close_demo_fires(db)
    db.close()
    client = _client()
    rca = client.post("/demo-host", follow_redirects=False)
    assert rca.status_code == 303
    assert "/incidents/INC-" in (rca.headers.get("location") or "")
    db = SessionLocal()
    _close_demo_fires(db)
    db.close()


def test_cli_board_prints_demo_on_demo_asset():
    rows = [
        {
            "number": "INC-000042",
            "status": "OPEN",
            "severity": "WARNING",
            "title": "High CPU",
            "demo": True,
            "asset": {"hostname": "forge-demo-01", "asset_id": "forge-demo-01"},
        }
    ]
    text = format_board(rows, color=False)
    assert "DEMO High CPU" in text
    detail = format_detail({**rows[0], "notifications": [], "audit": [], "notes": []}, color=False)
    assert "DEMO" in detail
    assert "INC-000042" in detail
