from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import asset_type_abbrev, similar_incident_groups
from app.main import app
from app.models import Asset, Notification, User
from app.security import hash_password
from app.seed import DEMO_ASSET, DEMO_OWNER_EMAIL, DEMO_WIN_ASSET, seed
from app.services import ingest_alertmanager, run_demo

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
    stale = client.get("/assets/app-lab-01", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/assets?edit=app-lab-01"
    page = client.get("/assets?edit=app-lab-01")
    assert page.status_code == 200
    assert b"payments@dc.local" in page.content
    listed = client.get("/assets")
    assert b'placeholder="Search any field"' in listed.content
    assert b'data-asset-filter="' not in listed.content
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
    page = client.get("/assets?edit=app-lab-01")
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
    stale = client.get(f"/assets/{DEMO_ASSET}", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == f"/assets?edit={DEMO_ASSET}"
    asset_page = client.get(f"/assets?edit={DEMO_ASSET}")
    assert asset_page.status_code == 200
    assert b"Edit asset" in asset_page.content
    assert b"<h2>Incidents</h2>" not in asset_page.content
    incident_page = client.get(f"/incidents/{incident.number}")
    assert b"Who to call" in incident_page.content
    assert b"Similar on this asset" in incident_page.content
    assert b"<h2>Incidents</h2>" not in incident_page.content
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
    assert note.subject == f"Alarm {DEMO_ASSET}"
    assert not note.subject.startswith("[DEMO]")
    db.close()


def test_role_labels_mention_analyst_inventory():
    from app.security import can, role_label

    analyst = SimpleNamespace(role="analyst")
    assert can(analyst, "write_assets")
    assert role_label("analyst") == "Analyst"



def test_assets_list_shows_edit_clone_remove():
    db = _db()
    db.add(
        User(
            email="ana-inv@forgesre.local",
            name="Ana",
            password_hash=hash_password("testpass"),
            role="analyst",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "ana-inv@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/assets")
    assert page.status_code == 200
    assert b"Add asset" not in page.content
    assert b">View<" not in page.content
    assert b">Edit<" in page.content
    assert b">Clone<" in page.content
    assert b">Verify<" in page.content
    assert b">Remove<" in page.content
    assert b">#</" in page.content or b"Asset number" in page.content or b">#</th>" in page.content
    assert b'placeholder="Search any field"' in page.content
    assert b'data-asset-filter="' not in page.content
    assert b'name="scrape_address"' not in page.content
    assert b'action="/assets/open-view"' not in page.content
    assert b'action="/assets/open-edit"' in page.content
    assert b"reach-sq" in page.content
    assert b">ICMP<" in page.content
    assert b'class="col-port"' in page.content
    assert ">L1–L4<" in page.text
    chips = page.text.split('aria-label="Asset filters"', 1)[1].split("</nav>", 1)[0]
    for label in (">All<", ">Critical<", ">Warning<", ">Offline<"):
        assert label in chips
    assert "Offline / unreachable" not in chips
    assert "No owner email" not in chips
    assert "flag=no-email" not in chips
    assert "flag=unreachable" in chips
    table = page.text.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
    assert 'class="asset-actions"' not in table
    assert "/assets?edit=" in table
    listed = client.get("/assets?edit=forge-demo-01")
    assert listed.status_code == 200
    assert b"Edit asset" in listed.content
    assert b'data-asset-graphs' in listed.content
    assert b'data-asset="forge-demo-01"' in listed.content
    assert b"asset-edit-graphs" in listed.content
    assert b'data-asset-glance=' not in listed.content
    viewed = client.get("/assets?view=forge-demo-01")
    assert viewed.status_code == 200
    assert b"Edit asset" in viewed.content
    assert b'data-asset-graphs' in viewed.content
    assert b'data-asset-glance=' not in viewed.content
    assert b'name="hostname"' in listed.content
    assert b'name="asset_id"' in listed.content
    assert b"readonly" in listed.content
    assert b">Verify<" in listed.content
    assert b">Remove<" in listed.content
    assert b"/assets/forge-demo-01/verify" in listed.content
    assert b">Lab<" not in listed.content
    clone_page = client.get("/assets?clone=forge-demo-01")
    assert clone_page.status_code == 200
    assert b"Clone asset" in clone_page.content
    assert b"copy-01" in clone_page.content
    assert b"forge-demo-" in clone_page.content
    db.close()


def test_assets_pager_opens_one_row_and_flashes_otherwise():
    db = _db()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    viewed = client.post(
        "/assets/open-view",
        data={"selected": DEMO_ASSET, "stay": "/assets?q=forge"},
        follow_redirects=False,
    )
    assert viewed.status_code == 303
    assert viewed.headers["location"].startswith("/assets?")
    assert f"edit={DEMO_ASSET}" in viewed.headers["location"]
    assert "q=forge" in viewed.headers["location"]
    one = client.post(
        "/assets/open-edit",
        data={"selected": DEMO_ASSET, "stay": "/assets?q=forge"},
        follow_redirects=False,
    )
    assert one.status_code == 303
    assert one.headers["location"].startswith("/assets?")
    assert f"edit={DEMO_ASSET}" in one.headers["location"]
    assert "q=forge" in one.headers["location"]
    cloned = client.post(
        "/assets/open-clone",
        data={"selected": DEMO_ASSET, "stay": "/assets?status=warning"},
        follow_redirects=False,
    )
    assert cloned.status_code == 303
    assert f"clone={DEMO_ASSET}" in cloned.headers["location"]
    assert "status=warning" in cloned.headers["location"]
    verified = client.post("/assets/open-verify", data={"selected": DEMO_ASSET}, follow_redirects=False)
    assert verified.status_code == 303
    assert verified.headers["location"] == f"/assets/verify?selected={DEMO_ASSET}"
    pair = client.post(
        "/assets/open-verify",
        data={"selected": [DEMO_ASSET, DEMO_WIN_ASSET]},
        follow_redirects=False,
    )
    assert pair.status_code == 303
    assert "edit_note=select-one" not in pair.headers["location"]
    assert f"selected={DEMO_ASSET}" in pair.headers["location"]
    assert f"selected={DEMO_WIN_ASSET}" in pair.headers["location"]
    many = client.post(
        "/assets/open-edit",
        data={"selected": [DEMO_ASSET, "forge-demo-02"], "stay": "/assets?flag=unreachable"},
        follow_redirects=False,
    )
    assert many.status_code == 303
    assert "edit_note=select-one" in many.headers["location"]
    assert "flag=unreachable" in many.headers["location"]
    assert "edit=" not in many.headers["location"].split("edit_note", 1)[0]
    flashed = client.get(many.headers["location"])
    assert 'role="status">Select one' in flashed.text
    none = client.post("/assets/open-clone", data={"stay": "https://evil.example/assets"}, follow_redirects=False)
    assert none.status_code == 303
    assert none.headers["location"] == "/assets?edit_note=select-one"
    db.add(
        User(
            email="view-pager@forgesre.local",
            name="View",
            password_hash=hash_password("testpass"),
            role="viewer",
        )
    )
    db.commit()
    viewer = TestClient(app)
    viewer.post("/login", data={"email": "view-pager@forgesre.local", "password": "testpass"}, follow_redirects=False)
    for path in ("/assets/open-edit", "/assets/open-clone", "/assets/open-verify", "/assets/bulk-delete"):
        assert viewer.post(path, data={"selected": DEMO_ASSET}, follow_redirects=False).status_code == 403
    opened = viewer.post("/assets/open-view", data={"selected": DEMO_ASSET}, follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == f"/assets?edit={DEMO_ASSET}"
    listed = viewer.get("/assets")
    assert 'action="/assets/open-view"' not in listed.text
    assert 'action="/assets/open-edit"' not in listed.text
    assert 'action="/assets/bulk-delete"' not in listed.text
    assert 'action="/assets/export"' in listed.text
    assert f'data-list-view="/assets?edit={DEMO_ASSET}"' in listed.text
    pane = viewer.get(f"/assets?view={DEMO_ASSET}")
    assert pane.status_code == 200
    assert "View asset" in pane.text
    assert "Edit asset" not in pane.text
    assert 'id="asset-form"' in pane.text
    assert "asset-form-view" in pane.text
    assert " inert" in pane.text
    assert "data-asset-graphs" in pane.text
    assert "data-asset-glance" not in pane.text
    assert ">Save<" not in pane.text.split("asset-form-actions", 1)[1]
    assert ">Remove<" not in pane.text.split("asset-form-actions", 1)[1]
    assert ">Verify<" not in pane.text.split("asset-form-actions", 1)[1]
    assert ">Close<" in pane.text.split("asset-form-actions", 1)[1]
    denied = viewer.post(
        f"/assets/{DEMO_ASSET}/update",
        data={"hostname": "nope", "ip": "10.20.30.40", "type": "Linux Server"},
        follow_redirects=False,
    )
    assert denied.status_code == 403
    db.close()


def test_asset_type_abbrev_map():
    assert asset_type_abbrev("Linux Server") == "lnx"
    assert asset_type_abbrev("Windows Server") == "win"
    assert asset_type_abbrev("Web/appliance") == "web"
    assert asset_type_abbrev("Network device") == "net"
    assert asset_type_abbrev("Network Switch") == "net"
    assert asset_type_abbrev("Auto (detect exporter)") == "Auto (detect exporter)"
    assert asset_type_abbrev("") == "—"
    assert asset_type_abbrev("Custom Box") == "Custom Box"


def test_assets_table_abbreviates_type_form_keeps_full_names():
    db = _db()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/assets")
    assert page.status_code == 200
    table = page.text.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
    rest = client.get("/discovery").text
    assert 'title="Linux Server">lnx</span>' in table
    assert 'title="Windows Server">win</span>' in table
    assert 'title="Network Switch">net</span>' in table
    assert ">Linux Server<" not in table
    assert ">Windows Server<" not in table
    assert ">Network Switch<" not in table
    assert 'value="Linux Server"' in rest
    assert 'value="Windows Server"' in rest
    assert 'value="Network device"' in rest
    assert 'value="Web/appliance"' in rest
    assert ">Linux Server</option>" in rest
    assert ">Windows Server</option>" in rest
    assert ">Network device</option>" in rest
    assert ">Web/appliance</option>" in rest
    db.close()


def test_asset_table_actions_live_in_the_pager():
    html = Path("frontend/templates/assets.html").read_text(encoding="utf-8")
    css = Path("frontend/static/app.css").read_text(encoding="utf-8")
    base = Path("frontend/templates/base.html").read_text(encoding="utf-8")
    table = html.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
    assert 'td class="row-actions"' not in table
    assert 'class="asset-actions"' not in table
    assert "asset_type_abbrev" in table
    assert 'class="col-enum' in table
    assert 'class="col-host"' in table
    pager = html.split("list_actions=", 1)[1]
    assert '"/assets/open-view"' not in pager
    assert '"/assets/open-edit"' in pager
    assert '"/assets/open-clone"' in pager
    assert '"/assets/open-verify"' in pager
    assert '"/assets/export"' in pager
    assert '"/assets/bulk-delete"' in pager
    assert '"delete_last": True' in pager
    assert "vertical-align: middle" in css
    assert ".asset-table { table-layout: auto;" in css
    assert ".evidence-table { table-layout: fixed; }" in css
    assert "app.css?v=v09-44" in base


def test_viewer_cannot_see_asset_write_actions():
    db = _db()
    db.add(
        User(
            email="view-inv@forgesre.local",
            name="View",
            password_hash=hash_password("testpass"),
            role="viewer",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "view-inv@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/assets")
    assert b"Add asset" not in page.content
    assert b">Remove<" not in page.content
    assert b">Remove<" not in page.content
    assert b">Verify<" not in page.content
    assert b'class="col-port"' in page.content
    assert b"reach-sq icmp ping" in page.content
    assert b">ICMP<" in page.content
    blocked = client.post("/assets/forge-demo-01/delete", follow_redirects=False)
    assert blocked.status_code == 403
    verify_blocked = client.get("/assets/forge-demo-01/verify", follow_redirects=False)
    assert verify_blocked.status_code == 403
    db.close()


def test_edit_hostname_and_scrape_rewrites_http_sd():
    from app.inventory import create_manual_asset, sd_targets
    from app.seed import DEMO_ASSET

    db = _db()
    host = create_manual_asset(
        db,
        hostname="sd-edit-01",
        ip="10.55.8.10",
        type="Linux Server",
        actor="tester",
    )
    assert host.scrape_address == "10.55.8.10:9100"
    assert any("10.55.8.10:9100" in item["targets"][0] for item in sd_targets(db))
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    edited = client.post(
        "/assets/sd-edit-01/update",
        data={
            "hostname": "sd-edit-renamed",
            "ip": "10.55.8.11",
            "type": "Windows Server",
            "environment": "Production",
            "owner": "platform",
            "contact_name": "",
            "owner_email": "",
            "owner_phone": "",
            "notes": "",
            "scrape_address": "10.55.8.11:9182",
        },
        follow_redirects=False,
    )
    assert edited.status_code in {302, 303}
    db.expire_all()
    row = db.query(Asset).filter_by(asset_id="sd-edit-01").one()
    assert row.hostname == "sd-edit-renamed"
    assert row.asset_id == "sd-edit-01"
    assert row.type == "Windows Server"
    assert row.scrape_address == "10.55.8.11:9182"
    targets = sd_targets(db)
    assert any(item["labels"]["asset"] == "sd-edit-01" and item["targets"] == ["10.55.8.11:9182"] for item in targets)
    assert not any("10.55.8.10:9100" in item["targets"][0] for item in targets)
    assert not any(item["labels"]["asset"] == DEMO_ASSET for item in targets)
    db.close()


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


def test_remove_asset_unlinks_incidents_and_drops_sd():
    from app.inventory import create_manual_asset, sd_targets
    from app.models import Incident
    from app.services import ingest_alertmanager

    db = _db()
    host = create_manual_asset(
        db,
        hostname="gone-host-01",
        ip="10.55.8.50",
        type="Linux Server",
        actor="tester",
    )
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "severity": "warning", "asset": host.asset_id},
                    "annotations": {"summary": "High CPU", "description": "CPU 94%"},
                    "fingerprint": "test-delete-asset",
                }
            ],
        },
    )
    assert created
    incident_number = created[0].number
    assert any(item["labels"]["asset"] == "gone-host-01" for item in sd_targets(db))
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    removed = client.post("/assets/gone-host-01/delete", follow_redirects=False)
    assert removed.status_code in {302, 303}
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id="gone-host-01").first() is None
    assert not any(item["labels"]["asset"] == "gone-host-01" for item in sd_targets(db))
    inc = db.query(Incident).filter_by(number=incident_number).one()
    assert inc.asset_id is None
    assert inc.title
    db.close()


def test_remove_demo_asset_and_seed_does_not_restore():
    from app.inventory import sd_targets
    from app.seed import seed

    db = _db()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/assets")
    assert b'action="/assets/bulk-delete"' in page.content
    assert b">Remove<" in page.content
    edit = client.get("/assets?edit=forge-demo-01")
    assert b'action="/assets/forge-demo-01/delete"' in edit.content
    demo = client.post("/assets/forge-demo-01/delete", follow_redirects=False)
    assert demo.status_code in {302, 303}
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id="forge-demo-01").first() is None
    assert not any(item["labels"]["asset"] == "forge-demo-01" for item in sd_targets(db))
    api_gone = client.post("/api/v1/assets/forge-demo-01/delete")
    assert api_gone.status_code == 404
    seed(db)
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id="forge-demo-01").first() is None
    assert db.query(Asset).filter_by(asset_id="forge-demo-win-01").one()
    from app.models import AuditLog

    db.query(AuditLog).filter_by(action="asset.delete", object_id="forge-demo-01").delete()
    db.commit()
    seed(db)
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    db.close()


def test_reachability_api_refreshes_without_blocking_list(monkeypatch):
    from app.asset_probe import AssetProbe, CheckResult

    def fake_probe(item, timeout=0.8, **kwargs):
        ok = str(item.get("asset_id") or "") == "forge-demo-01"
        return AssetProbe(
            asset_id=item["asset_id"],
            hostname=item.get("hostname") or "",
            ip=item.get("ip") or "",
            kind="linux",
            type=item.get("type") or "",
            scrape=item.get("scrape_address") or "",
            port=9100,
            icmp=CheckResult("icmp", ok, "reachable" if ok else "no reply"),
            metrics=CheckResult("metrics", ok, "node_exporter :9100/metrics" if ok else "fail"),
        )

    monkeypatch.setattr("app.asset_probe.probe_target", fake_probe)
    db = _db()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    listed = client.get("/assets")
    assert listed.status_code == 200
    assert b"reach-sq icmp ping yellow" in listed.content
    rows = client.get("/api/v1/assets/reachability").json()
    demo = next(item for item in rows if item["asset_id"] == "forge-demo-01")
    assert demo["ping"] == "green"
    assert demo["exporter"] == "green"
    assert demo["exporter_label"] in {":9100", "exp."}
    cached = client.get("/api/v1/assets/reachability", params={"refresh": "false"}).json()
    demo_cached = next(item for item in cached if item["asset_id"] == "forge-demo-01")
    assert demo_cached["ping"] == "green"
    db.close()


def test_assets_legend_explains_yellow_ping_when_scrape_works():
    db = _db()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/assets")
    assert page.status_code == 200
    thead = page.text.split('<table class="asset-table"', 1)[1].split("</thead>", 1)[0]
    assert ">Port<" in thead
    assert 'class="info-tip"' not in thead
    assert "ICMP failed but exporter/SNMP" not in thead
    form = (ROOT / "frontend" / "templates" / "_asset_form.html").read_text(encoding="utf-8")
    assert "ICMP ping is not a scrape" not in form
    assert 'include "_info_tip.html"' not in form
    db.close()


def test_reachability_windows_icmp_blocked_exporter_up_is_yellow(monkeypatch):
    from app.asset_probe import AssetProbe, CheckResult
    from app.inventory import create_manual_asset

    def fake_probe(item, timeout=0.8, **kwargs):
        win = "windows" in str(item.get("type") or "").lower()
        return AssetProbe(
            asset_id=item["asset_id"],
            hostname=item.get("hostname") or "",
            ip=item.get("ip") or "",
            kind="windows" if win else "linux",
            type=item.get("type") or "",
            scrape=item.get("scrape_address") or "",
            port=9182 if win else 9100,
            icmp=CheckResult("icmp", False if win else True, "no reply" if win else "reachable"),
            metrics=CheckResult(
                "metrics",
                True,
                "windows_exporter :9182/metrics" if win else "node_exporter :9100/metrics",
            ),
        )

    monkeypatch.setattr("app.asset_probe.probe_target", fake_probe)
    db = _db()
    host = create_manual_asset(
        db,
        hostname="DESKTOP-CG81N3J",
        ip="10.89.11.60",
        type="Windows Server",
        actor="tester",
    )
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    rows = client.get("/api/v1/assets/reachability").json()
    win = next(item for item in rows if item["asset_id"] == host.asset_id)
    assert win["ping"] == "yellow"
    assert win["exporter"] == "green"
    assert "9182" in (win.get("exporter_label") or "")
    db.expire_all()
    stored = db.query(Asset).filter_by(asset_id=host.asset_id).one()
    assert stored.ping_status == "yellow"
    listed = client.get(f"/assets?q={host.asset_id}")
    assert listed.status_code == 200
    stale = client.get(f"/assets/{host.asset_id}", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == f"/assets?edit={host.asset_id}"
    assert b"reach-sq icmp ping yellow" in listed.content
    db.close()


def test_asset_numbers_are_stable_searchable_and_not_reused():
    from sqlalchemy import text

    from app.inventory import create_manual_asset, delete_asset
    from app.migrate import migrate

    db = _db()
    rows = db.query(Asset).order_by(Asset.created_at, Asset.id).all()
    assert all(row.number for row in rows)
    assert len({row.number for row in rows}) == len(rows)
    demo = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    assert demo.number
    current_max = max(int(row.number) for row in rows)
    host = create_manual_asset(
        db,
        hostname="unique-num-host",
        ip="10.200.200.200",
        type="Linux Server",
        actor="tester",
    )
    assert host.number == current_max + 1
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    listed = client.get("/assets")
    assert listed.status_code == 200
    assert b">#</th>" in listed.content
    found = client.get(f"/assets?q={host.number}")
    assert found.status_code == 200
    assert str(host.number).encode() in found.content
    assert b"unique-num-host" in found.content
    api = client.get("/api/v1/assets").json()
    match = next(item for item in api if item["asset_id"] == "unique-num-host")
    assert match["number"] == host.number
    stale = client.get("/assets/unique-num-host", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/assets?edit=unique-num-host"
    edit = client.get("/assets?edit=unique-num-host")
    assert edit.status_code == 200
    assert str(host.number).encode() in found.content

    taken = host.number
    delete_asset(db, host, actor="tester")
    nxt = create_manual_asset(
        db,
        hostname="after-gap-host",
        ip="10.200.200.201",
        type="Linux Server",
        actor="tester",
    )
    assert nxt.number == taken + 1

    db.execute(text("UPDATE assets SET number = NULL"))
    db.commit()
    migrate(engine)
    db.expire_all()
    restored = db.query(Asset).order_by(Asset.created_at, Asset.id).all()
    assert all(row.number for row in restored)
    assert len({row.number for row in restored}) == len(restored)
    assert restored[0].number == 1
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


def test_html_add_posts_asset_id_edit_keeps_it():
    db = _db()
    db.add(
        User(
            email="id-first@forgesre.local",
            name="Ida",
            password_hash=hash_password("testpass"),
            role="analyst",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "id-first@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    add = client.get("/discovery")
    assert add.status_code == 200
    form_at = add.text.find('id="asset-form"')
    id_at = add.text.find('name="asset_id"', form_at)
    host_at = add.text.find('name="hostname"', form_at)
    ip_at = add.text.find('name="ip"', form_at)
    assert 0 < id_at < host_at < ip_at
    created = client.post(
        "/assets",
        data={
            "asset_id": "WIN10-GP",
            "hostname": "DESKTOP-CG81N3J",
            "ip": "10.66.10.14",
            "type": "Windows Server",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    assert "edit=win10-gp" in created.headers.get("location", "")
    stale = client.get("/assets/win10-gp", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/assets?edit=win10-gp"
    page = client.get("/assets?edit=win10-gp")
    assert page.status_code == 200
    assert b"DESKTOP-CG81N3J" in page.content
    assert b"win10-gp" in page.content
    edited = client.post(
        "/assets/win10-gp/update",
        data={
            "asset_id": "should-not-apply",
            "hostname": "DESKTOP-CG81N3J",
            "ip": "10.66.10.15",
            "type": "Windows Server",
        },
        follow_redirects=False,
    )
    assert edited.status_code in {302, 303}
    body = client.get("/api/v1/assets/win10-gp")
    assert body.status_code == 200
    data = body.json()
    assert data["asset_id"] == "win10-gp"
    assert data["hostname"] == "DESKTOP-CG81N3J"
    assert data["ip"] == "10.66.10.15"
    missing = client.get("/api/v1/assets/should-not-apply")
    assert missing.status_code == 404
    edit_form = client.get("/assets?edit=win10-gp")
    assert "Set at add; cannot change" not in edit_form.text
    assert "readonly" in edit_form.text
    db.close()

