from fastapi.testclient import TestClient

from app.cli_view import format_board, format_detail
from app.db import Base, SessionLocal, engine
from app.inventory import is_snmp_asset, sd_snmp_targets
from app.main import app
from app.models import Asset, Job
from app.seed import DEMO_ASSET, DEMO_SW_ASSET, DEMO_WIN_ASSET, seed
from app.services import (
    close_open_incidents,
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

