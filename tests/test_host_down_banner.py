from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Incident, User
from app.security import hash_password
from app.seed import DEMO_ASSET, DEMO_SW_ASSET, DEMO_WIN_ASSET, seed
from app.services import (
    close_open_incidents,
    ingest_alertmanager,
    is_host_down_incident,
    list_host_down_incidents,
    run_demo,
    run_demo_host,
    run_demo_network,
)


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    login = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert login.status_code in {302, 303}


def _close_down(db):
    close_open_incidents(db, f"NodeExporterDown:{DEMO_ASSET}", include_resolved=True)
    close_open_incidents(db, f"WindowsExporterDown:{DEMO_WIN_ASSET}", include_resolved=True)
    close_open_incidents(db, f"SnmpDeviceUnreachable:{DEMO_SW_ASSET}", include_resolved=True)
    close_open_incidents(db, f"HighCPU:{DEMO_ASSET}", include_resolved=True)
    db.commit()


def test_host_down_helper_matches_exporter_and_snmp_only():
    linux = Incident(
        number="INC-9001_01.01.2026_00:00",
        title="Host unreachable",
        fingerprint=f"NodeExporterDown:{DEMO_ASSET}",
        alert_payload={"labels": {"alertname": "NodeExporterDown", "asset": DEMO_ASSET}},
        status="OPEN",
    )
    cpu = Incident(
        number="INC-9002_01.01.2026_00:00",
        title="High CPU",
        fingerprint=f"HighCPU:{DEMO_ASSET}",
        alert_payload={"labels": {"alertname": "HighCPU", "asset": DEMO_ASSET}},
        status="OPEN",
    )
    assert is_host_down_incident(linux) is True
    assert is_host_down_incident(cpu) is False
    assert is_host_down_incident(None) is False


def test_dashboard_host_down_banner_lists_open_incidents_with_demo():
    db = _db()
    _close_down(db)
    client = TestClient(app)
    _login(client)
    empty = client.get("/")
    assert empty.status_code == 200
    assert b'id="host-down-banner"' in empty.content
    html = empty.text
    banner_at = html.find('id="host-down-banner"')
    assert banner_at > 0
    assert "hidden" in html[banner_at : banner_at + 80]
    assert "HOST DOWN" in html

    cpu = run_demo(db)
    assert cpu is not None
    still = client.get("/")
    assert b"High CPU" in still.content
    listed = still.text
    banner_at = listed.find('id="host-down-banner"')
    hidden_at = listed.find("hidden", banner_at, banner_at + 80) if banner_at >= 0 else -1
    assert banner_at > 0
    assert hidden_at > banner_at
    api_empty = client.get("/api/v1/incidents/down")
    assert api_empty.status_code == 200
    assert api_empty.json() == []

    host = run_demo_host(db)
    net = run_demo_network(db)
    assert host is not None and net is not None
    assert is_host_down_incident(host)
    assert is_host_down_incident(net)
    down = list_host_down_incidents(db)
    numbers = {row.number for row in down}
    assert host.number in numbers
    assert net.number in numbers
    assert cpu.number not in numbers

    home = client.get("/")
    html = home.text
    assert home.status_code == 200
    assert 'id="host-down-banner"' in html
    assert "HOST DOWN" in html
    assert "2 open incidents" in html
    assert f'href="/incidents/{host.number}"' in html
    assert f'href="/incidents/{net.number}"' in html
    assert html.count('id="host-down-banner"') == 1
    banner = html[html.find('id="host-down-banner"') : html.find("<section>")]
    assert "DEMO" in banner
    assert cpu.number not in banner

    payload = client.get("/api/v1/incidents/down").json()
    ids = {row["number"] for row in payload}
    assert host.number in ids and net.number in ids
    assert cpu.number not in ids
    assert any(row["demo"] is True and row["number"] == host.number for row in payload)

    close_open_incidents(db, f"NodeExporterDown:{DEMO_ASSET}", include_resolved=True)
    close_open_incidents(db, f"SnmpDeviceUnreachable:{DEMO_SW_ASSET}", include_resolved=True)
    db.commit()
    after = client.get("/")
    after_html = after.text
    banner_at = after_html.find('id="host-down-banner"')
    assert "hidden" in after_html[banner_at : banner_at + 80]
    assert client.get("/api/v1/incidents/down").json() == []
    db.close()


def test_viewer_sees_host_down_banner_and_asset_pills():
    db = _db()
    _close_down(db)
    if db.query(User).filter_by(email="view-down@forgesre.local").first() is None:
        db.add(
            User(
                email="view-down@forgesre.local",
                name="View",
                password_hash=hash_password("testpass"),
                role="viewer",
            )
        )
        db.commit()
    host = run_demo_host(db)
    assert host is not None
    client = TestClient(app)
    _login(client, "view-down@forgesre.local")
    home = client.get("/")
    assert home.status_code == 200
    assert b'id="host-down-banner"' in home.content
    assert host.number.encode() in home.content
    assert b"DEMO" in home.content
    assets = client.get("/assets")
    assert assets.status_code == 200
    assert b"Ping / comms" in assets.content
    assert b"reach-dot ping" in assets.content
    assert b">Ping<" in assets.content
    detail = client.get(f"/assets/{DEMO_ASSET}")
    assert detail.status_code == 200
    assert b"reach-dot ping" in detail.content
    down = client.get("/api/v1/incidents/down")
    assert down.status_code == 200
    assert any(row["number"] == host.number for row in down.json())
    db.close()


def test_windows_exporter_down_counts_as_host_down():
    db = _db()
    _close_down(db)
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "WindowsExporterDown",
                        "severity": "warning",
                        "asset": DEMO_WIN_ASSET,
                    },
                    "annotations": {"summary": "windows_exporter down"},
                }
            ],
        },
    )
    assert created
    row = created[0]
    assert is_host_down_incident(row)
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert row.number.encode() in home.content
    assert b"HOST DOWN" in home.content
    db.close()
