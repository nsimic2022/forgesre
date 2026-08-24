from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import similar_incident_groups
from app.main import app
from app.models import Asset, Notification, User
from app.security import hash_password
from app.seed import DEMO_ASSET, DEMO_OWNER_EMAIL, seed
from app.services import ingest_alertmanager, run_demo


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


def test_analyst_can_create_and_edit_asset_contacts():
    db = _db()
    db.add(
        User(
            email="analyst@forgesre.local",
            name="Ana",
            password_hash=hash_password("testpass"),
            role="analyst",
        )
    )
    db.add(
        User(
            email="viewer@forgesre.local",
            name="View",
            password_hash=hash_password("testpass"),
            role="viewer",
        )
    )
    db.commit()

    client = TestClient(app)
    denied = client.post(
        "/login",
        data={"email": "viewer@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert denied.status_code in {302, 303}
    blocked = client.post("/assets", data={"hostname": "should-fail"}, follow_redirects=False)
    assert blocked.status_code == 403
    client.post("/logout")

    login = client.post(
        "/login",
        data={"email": "analyst@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    created = client.post(
        "/assets",
        data={
            "hostname": "app-lab-01",
            "ip": "10.10.10.50",
            "type": "Linux Server",
            "environment": "Production",
            "owner": "payments",
            "contact_name": "Milan",
            "owner_email": "payments@dc.local",
            "owner_phone": "+381-11-555-0101",
            "notes": "Primary payments host",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    page = client.get("/assets/app-lab-01")
    assert page.status_code == 200
    assert b"payments@dc.local" in page.content
    form = client.get("/assets")
    assert b"Windows Server" in form.content
    assert b"Auto (detect exporter)" in form.content
    assert b"+381-11-555-0101" in page.content
    assert b"Milan" in page.content

    edited = client.post(
        "/assets/app-lab-01/update",
        data={
            "ip": "10.10.10.51",
            "type": "Linux Server",
            "environment": "Staging",
            "owner": "payments",
            "contact_name": "Milan",
            "owner_email": "milan@dc.local",
            "owner_phone": "+381-11-555-0101",
            "notes": "Moved",
            "scrape_address": "10.10.10.51:9100",
        },
        follow_redirects=False,
    )
    assert edited.status_code in {302, 303}
    page = client.get("/assets/app-lab-01")
    assert b"milan@dc.local" in page.content
    assert b"Staging" in page.content
    body = client.get("/api/v1/assets/app-lab-01")
    assert body.status_code == 200
    data = body.json()
    assert data["owner_email"] == "milan@dc.local"
    assert data["ip"] == "10.10.10.51"
    db.close()


def test_notification_uses_asset_owner_email_and_demo_history():
    from app.inventory import create_manual_asset

    db = _db()
    host = create_manual_asset(
        db,
        hostname="notify-host-01",
        ip="10.10.10.77",
        owner="payments",
        contact_name="Milan",
        owner_email="payments@dc.local",
        owner_phone="+381-11-555-0101",
        actor="tester",
    )
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "severity": "warning", "asset": host.asset_id},
                "annotations": {"summary": "High CPU", "description": "CPU 94%"},
                "fingerprint": "test-owner-mail",
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    assert created
    incident = created[0]
    note = (
        db.query(Notification)
        .filter_by(incident_id=incident.id, step_key="immediate")
        .first()
    )
    assert note is not None
    assert note.target == "payments@dc.local"
    assert "Milan" in note.body
    assert "+381-11-555-0101" in note.body
    assert "policy role: team" in note.body

    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    home = client.get("/")
    assert home.status_code == 200
    assert b'id="demo-open"' in home.content
    assert b'id="demo-panel"' in home.content
    assert b"First-hour walkthrough" not in home.content
    asset_page = client.get(f"/assets/{DEMO_ASSET}")
    assert b"Similar incident" in asset_page.content
    assert b"High CPU" in asset_page.content or b"HighCPU" in asset_page.content
    incident_page = client.get(f"/incidents/{incident.number}")
    assert b"Who to call" in incident_page.content
    assert b"payments@dc.local" in incident_page.content
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
    assert note.subject.startswith("[DEMO]")
    db.close()


def test_role_labels_mention_analyst_inventory():
    from app.security import can, role_label

    analyst = SimpleNamespace(role="analyst")
    assert can(analyst, "write_assets")
    assert role_label("analyst") == "Analyst"
