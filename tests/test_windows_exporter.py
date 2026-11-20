
from app.db import Base, SessionLocal, engine
from app.inventory import (
    WINDOWS_EXPORTER_PORT,
    approve_candidate,
    asset_kind,
    create_manual_asset,
    is_snmp_asset,
    sd_snmp_targets,
    sd_targets,
    upsert_candidate,
)
from app.models import Asset, Playbook, Playrule
from app.seed import DEMO_WIN_ASSET, seed
from discovery import DEFAULT_PORTS, classify
from rca.collector import promql_queries_for


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_windows_server_is_not_classified_as_linux():
    assert asset_kind("Windows Server") == "windows"
    assert asset_kind("Linux Server") == "linux"
    assert asset_kind("Network device") == "network"


def test_create_windows_asset_scrapes_windows_exporter_9182():
    db = _db()
    win = create_manual_asset(
        db,
        hostname="win-prod-01",
        ip="10.88.10.60",
        type="Windows Server",
        owner="payments",
        owner_email="payments@dc.local",
        actor="tester",
    )
    linux = create_manual_asset(
        db,
        hostname="lnx-prod-01",
        ip="10.88.10.50",
        type="Linux Server",
        actor="tester",
    )
    assert win.monitoring_profile == "windows-standard"
    assert win.scrape_address == "10.88.10.60:9182"
    assert linux.monitoring_profile == "linux-standard"
    assert linux.scrape_address == "10.88.10.50:9100"
    assert is_snmp_asset(win) is False

    targets = sd_targets(db)
    win_row = next(item for item in targets if item["labels"]["asset"] == "win-prod-01")
    linux_row = next(item for item in targets if item["labels"]["asset"] == "lnx-prod-01")
    assert win_row["targets"] == ["10.88.10.60:9182"]
    assert win_row["labels"]["job"] == "windows-standard"
    assert linux_row["targets"] == ["10.88.10.50:9100"]
    assert linux_row["labels"]["job"] == "linux-standard"
    assert not any(item["labels"]["asset"] == DEMO_WIN_ASSET for item in targets)
    snmp_labels = [(item.get("labels") or {}).get("asset") for item in sd_snmp_targets(db)]
    assert "win-prod-01" not in snmp_labels
    db.close()


def test_demo_windows_asset_is_not_in_http_sd():
    db = _db()
    win = db.query(Asset).filter_by(asset_id=DEMO_WIN_ASSET).one()
    assert win.type == "Windows Server"
    assert win.scrape_address == ""
    assert win.monitoring_profile == "windows-lab"
    assert not any(item["labels"]["asset"] == DEMO_WIN_ASSET for item in sd_targets(db))
    db.close()


def test_discovery_9182_approves_windows_scrape():
    assert 9182 in DEFAULT_PORTS
    assert classify([9182]) == "Possible Windows server"
    db = _db()
    row = upsert_candidate(db, "10.9.9.82", "Possible Windows server", [9182])
    db.commit()
    asset = approve_candidate(db, row, actor="tester")
    assert asset.type == "Windows Server"
    assert asset.monitoring_profile == "windows-standard"
    assert asset.scrape_address == f"10.9.9.82:{WINDOWS_EXPORTER_PORT}"
    db.close()


def test_windows_playrules_and_alerts_are_seeded():
    db = _db()
    names = {row.name for row in db.query(Playrule).all()}
    assert "windows-cpu" in names
    assert "windows-exporter-down" in names
    assert "windows-filesystem" in names
    assert "windows-memory" in names
    down = db.query(Playrule).filter_by(name="windows-exporter-down").one()
    assert down.condition.get("alertname") == "WindowsExporterDown"
    memory = db.query(Playrule).filter_by(name="windows-memory").one()
    assert memory.condition.get("alertname") == "WindowsMemoryHigh"
    assert memory.condition.get("value") == 90
    book = db.query(Playbook).filter_by(id=memory.playbook_id).one()
    assert book.slug == "memory-high"
    db.close()


def test_promql_windows_uses_windows_exporter_metrics():
    queries = promql_queries_for({"asset_id": "win-prod-01", "type": "Windows Server"})
    blob = str(queries)
    assert "windows_cpu_time_total" in blob
    assert "windows_logical_disk" in blob
    assert "node_cpu_seconds_total" not in blob
    assert "forgesre_demo_cpu_percent" not in blob


