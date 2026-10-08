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


def test_dashboard_has_no_host_down_banner_but_api_lists_down_incidents():
    db = _db()
    _close_down(db)
    client = TestClient(app)
    _login(client)
    empty = client.get("/")
    assert empty.status_code == 200
    assert b'id="host-down-banner"' not in empty.content
    assert b"HOST DOWN" not in empty.content

    cpu = run_demo(db)
    assert cpu is not None
    assert client.get("/api/v1/incidents/down").json() == []

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
    assert 'id="host-down-banner"' not in html
    assert "data-host-down-banner" not in html
    assert "HOST DOWN" not in html
    assert "open incidents for unreachable host" not in html
    assert "data-incident-tile" in html

    payload = client.get("/api/v1/incidents/down").json()
    ids = {row["number"] for row in payload}
    assert host.number in ids and net.number in ids
    assert cpu.number not in ids
    assert any(row["demo"] is True and row["number"] == host.number for row in payload)

    close_open_incidents(db, f"NodeExporterDown:{DEMO_ASSET}", include_resolved=True)
    close_open_incidents(db, f"SnmpDeviceUnreachable:{DEMO_SW_ASSET}", include_resolved=True)
    db.commit()
    assert client.get("/api/v1/incidents/down").json() == []
    db.close()


def test_viewer_dashboard_without_host_down_banner_and_asset_pills():
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
    assert b'id="host-down-banner"' not in home.content
    assert b"HOST DOWN" not in home.content
    assets = client.get("/assets")
    assert assets.status_code == 200
    assert b"ICMP / port / SNMP" in assets.content
    assert b"reach-sq icmp ping" in assets.content
    assert b">ICMP<" in assets.content
    detail = client.get(f"/assets/{DEMO_ASSET}")
    assert detail.status_code == 200
    assert b"reach-sq icmp ping" in detail.content
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
    down = client.get("/api/v1/incidents/down").json()
    assert any(item["number"] == row.number for item in down)
    assert b"HOST DOWN" not in client.get("/").content
    db.close()
