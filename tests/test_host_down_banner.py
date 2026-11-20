from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Incident
from app.seed import DEMO_ASSET, DEMO_SW_ASSET, DEMO_WIN_ASSET, seed
from app.services import (
    close_open_incidents,
    ingest_alertmanager,
    is_host_down_incident,
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
