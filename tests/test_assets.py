from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import asset_type_abbrev, similar_incident_groups
from app.main import app
from app.models import Asset, Notification
from app.seed import DEMO_ASSET, DEMO_OWNER_EMAIL, seed
from app.services import run_demo

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_seeded_demo_asset_has_contacts_and_similar_history():
    db = _db()
    asset = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    assert asset.owner_email == DEMO_OWNER_EMAIL
    assert asset.contact_name
    assert asset.owner_phone
    groups = similar_incident_groups(db, asset)
    assert groups
    cpu = next(item for item in groups if item["alertname"] == "HighCPU")
    assert cpu["count"] >= 1
    assert cpu["incidents"][0]["status"] == "CLOSED"
    db.close()


def test_run_demo_keeps_similar_history_and_notifies_owner():
    db = _db()
    incident = run_demo(db)
    assert incident is not None
    asset = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    groups = similar_incident_groups(db, asset)
    cpu = next(item for item in groups if item["alertname"] == "HighCPU")
    assert cpu["count"] >= 2
    assert cpu["open_count"] >= 1
    note = (
        db.query(Notification)
        .filter_by(incident_id=incident.id, step_key="immediate")
        .first()
    )
    assert note is not None
    assert note.target == DEMO_OWNER_EMAIL
    assert note.subject == f"Alarm {DEMO_ASSET}"
    assert not note.subject.startswith("[DEMO]")
    db.close()


def test_inventory_edits_need_engineer():
    from app.security import can, role_label

    assert not can(SimpleNamespace(role="analyst"), "write_assets")
    assert can(SimpleNamespace(role="engineer"), "write_assets")
    assert role_label("analyst") == "Analyst"



def test_asset_type_abbrev_map():
    assert asset_type_abbrev("Linux Server") == "lnx"
    assert asset_type_abbrev("Windows Server") == "win"
    assert asset_type_abbrev("Web/appliance") == "web"
    assert asset_type_abbrev("Network device") == "net"
    assert asset_type_abbrev("Network Switch") == "net"
    assert asset_type_abbrev("Auto (detect exporter)") == "Auto (detect exporter)"
    assert asset_type_abbrev("") == "—"
    assert asset_type_abbrev("Custom Box") == "Custom Box"


def test_clone_demo_becomes_real_scrape_target():
    from app.inventory import sd_targets
    from app.seed import DEMO_ASSET, is_demo_asset_id

    db = _db()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    cloned = client.post(
        "/api/v1/assets/forge-demo-01/clone",
        json={"hostname": "copy-lab-01", "ip": "10.55.8.40", "type": "Linux Server"},
    )
    assert cloned.status_code == 200, cloned.text
    body = cloned.json()
    assert body["asset_id"] == "copy-lab-01"
    assert body["hostname"] == "copy-lab-01"
    assert not is_demo_asset_id(body["asset_id"])
    assert body["asset_id"] != DEMO_ASSET
    assert body["scrape_address"]
    targets = sd_targets(db)
    assert any(item["labels"]["asset"] == "copy-lab-01" for item in targets)
    assert not any(item["labels"]["asset"] == DEMO_ASSET for item in targets)
    db.close()


def test_clone_keeping_demo_name_stays_out_of_http_sd():
    from app.inventory import create_manual_asset, sd_targets, suggest_clone_hostname
    from app.seed import is_demo_asset_id

    db = _db()
    source = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    suggested = suggest_clone_hostname(db, source)
    assert suggested.startswith("copy-")
    assert not is_demo_asset_id(suggested)
    lying = create_manual_asset(
        db,
        hostname="forge-demo-clone-x",
        ip="10.55.8.41",
        type="Linux Server",
        scrape_address="10.55.8.41:9100",
        actor="tester",
        require_new=True,
        cloned_from="forge-demo-01",
    )
    assert is_demo_asset_id(lying.asset_id)
    assert not any(item["labels"]["asset"] == lying.asset_id for item in sd_targets(db))
    db.close()


def test_create_typed_asset_id_independent_of_hostname():
    from app.inventory import create_manual_asset, validate_asset_id

    assert validate_asset_id("WIN10-GP") == "win10-gp"
    try:
        validate_asset_id("win10_gp")
        raise AssertionError("underscore should be rejected")
    except ValueError as exc:
        assert "hyphen" in str(exc).lower() or "lowercase" in str(exc).lower()
    try:
        validate_asset_id("-win10")
        raise AssertionError("leading hyphen should be rejected")
    except ValueError:
        pass

    db = _db()
    host = create_manual_asset(
        db,
        hostname="DESKTOP-CG81N3J",
        asset_id="win10-gp",
        ip="10.66.10.12",
        type="Windows Server",
        actor="tester",
    )
    assert host.asset_id == "win10-gp"
    assert host.hostname == "DESKTOP-CG81N3J"
    try:
        create_manual_asset(
            db,
            hostname="OTHER-PC",
            asset_id="win10-gp",
            ip="10.66.10.13",
            type="Windows Server",
            actor="tester",
            require_new=True,
        )
        raise AssertionError("duplicate asset_id should be rejected")
    except ValueError as exc:
        assert "win10-gp" in str(exc)
    db.close()


