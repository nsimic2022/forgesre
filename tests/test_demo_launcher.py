from fastapi.testclient import TestClient

from app.cli_view import format_board, format_detail
from app.db import Base, SessionLocal, engine
from app.inventory import is_snmp_asset, sd_snmp_targets
from app.journal import list_entries
from app.main import app
from app.models import Asset, Job, Notification
from app.seed import DEMO_ASSET, DEMO_SW_ASSET, DEMO_WIN_ASSET, seed
from app.services import (
    close_open_incidents,
    is_demo_incident,
    is_demo_journal,
    run_demo,
    run_demo_host,
    run_demo_network,
    run_demo_nodecpu,
    run_demo_rca,
    run_demo_windows,
)


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
    close_open_incidents(db, f"NodeCPUHigh:{DEMO_ASSET}", include_resolved=True)
    close_open_incidents(db, f"WindowsCPUHigh:{DEMO_WIN_ASSET}", include_resolved=True)
    close_open_incidents(db, f"SnmpDeviceUnreachable:{DEMO_SW_ASSET}", include_resolved=True)
    (
        db.query(Job)
        .filter(Job.status.in_(["pending", "running"]))
        .update({"status": "done"}, synchronize_session=False)
    )
    db.commit()


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
    assert html.find("Windows CPU (lab)", panel_at) > panel_at
    assert html.find("Network SNMP unreachable (lab)", panel_at) > panel_at
    assert html.find("Linux NodeCPUHigh", panel_at) > panel_at
    assert html.find('action="/demo"', panel_at) > panel_at
    assert html.find('action="/demo-rca"', panel_at) > panel_at
    assert html.find('action="/demo-reset"', panel_at) > panel_at
    assert html.find('action="/demo-windows"', panel_at) > panel_at
    assert html.find('action="/demo-network"', panel_at) > panel_at
    assert html.find('action="/demo-nodecpu"', panel_at) > panel_at
    before = html[:panel_at]
    assert 'action="/demo"' not in before
    assert 'action="/demo-rca"' not in before
    infra = html.find(">Infrastructure<")
    assert infra > 0
    assert panel_at < infra
    assert "windows_exporter is scraping" not in html.lower()
    assert "forge-demo-win-01 is not scraped" in html.lower() or "lab only" in html.lower()
    assert "real SNMP scrape" not in html
    assert "Not a live SNMP walk" in html


def test_run_demo_panel_has_at_least_five_scenarios():
    _db().close()
    client = _client()
    html = client.get("/").text
    panel = html[html.find('id="demo-panel"') :]
    ids = [
        "demo-highcpu",
        "demo-disk",
        "demo-host",
        "demo-windows",
        "demo-network",
        "demo-nodecpu",
    ]
    for scenario_id in ids:
        assert f'id="{scenario_id}"' in panel
    assert panel.count('class="demo-scenario"') >= 5
    assert panel.count('type="submit"') >= 5


def test_windows_and_network_demo_assets_are_seeded():
    db = _db()
    win = db.query(Asset).filter_by(asset_id=DEMO_WIN_ASSET).one()
    sw = db.query(Asset).filter_by(asset_id=DEMO_SW_ASSET).one()
    assert win.type == "Windows Server"
    assert win.hostname == DEMO_WIN_ASSET
    assert win.scrape_address == ""
    assert sw.type == "Network Switch"
    assert sw.hostname == DEMO_SW_ASSET
    assert sw.scrape_address == ""
    assert is_snmp_asset(win) is False
    assert is_snmp_asset(sw) is False
    labels = [(item.get("labels") or {}).get("asset") for item in sd_snmp_targets(db)]
    assert DEMO_WIN_ASSET not in labels
    assert DEMO_SW_ASSET not in labels
    db.close()


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
    journal = list_entries(db, module="demo")
    assert any("DEMO" in (row.summary or "") and is_demo_journal(row) for row in journal)
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

    ops = client.get("/ops")
    assert ops.status_code == 200
    assert 'class="pill demo"' in ops.text or "[DEMO]" in ops.text

    dash = client.get("/")
    assert 'class="pill demo"' in dash.text

    journal_page = client.get("/journal")
    assert journal_page.status_code == 200
    assert 'class="pill demo"' in journal_page.text

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


def test_windows_and_network_demo_incidents_are_demo_tagged():
    db = _db()
    win = run_demo_windows(db)
    net = run_demo_network(db)
    cpu = run_demo_nodecpu(db)
    assert win is not None and net is not None and cpu is not None
    assert is_demo_incident(win) is True
    assert is_demo_incident(net) is True
    assert is_demo_incident(cpu) is True
    assert win.asset.asset_id == DEMO_WIN_ASSET
    assert net.asset.asset_id == DEMO_SW_ASSET
    assert cpu.asset.asset_id == DEMO_ASSET
    assert "WindowsCPUHigh" in (win.fingerprint or "")
    assert "SnmpDeviceUnreachable" in (net.fingerprint or "")
    assert "NodeCPUHigh" in (cpu.fingerprint or "")
    win_note = db.query(Notification).filter_by(incident_id=win.id, step_key="immediate").one()
    net_note = db.query(Notification).filter_by(incident_id=net.id, step_key="immediate").one()
    assert win_note.subject.startswith("[DEMO]")
    assert net_note.subject.startswith("[DEMO]")
    assert "DEMO incident on forge-demo-win-01" in (win_note.body or "")
    assert "DEMO incident on forge-demo-sw-01" in (net_note.body or "")
    assert "windows_exporter is scraping" not in (win.summary or "").lower()
    assert "real SNMP scrape" not in (net.summary or "")
    numbers = {win.number, net.number, cpu.number}
    _close_demo_fires(db)
    db.close()

    client = _client()
    posted = client.post("/demo-windows", follow_redirects=False)
    assert posted.status_code == 303
    assert "/incidents/INC-" in (posted.headers.get("location") or "")
    net_post = client.post("/demo-network", follow_redirects=False)
    assert net_post.status_code == 303
    listing = client.get("/incidents")
    for number in numbers:
        assert number in listing.text
    assert listing.text.count("DEMO") >= 2
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
    win_board = format_board(
        [
            {
                "number": "INC-000043",
                "status": "OPEN",
                "severity": "WARNING",
                "title": "Windows CPU high (lab)",
                "asset": {"hostname": DEMO_WIN_ASSET, "asset_id": DEMO_WIN_ASSET},
            }
        ],
        color=False,
    )
    assert "DEMO Windows CPU" in win_board

