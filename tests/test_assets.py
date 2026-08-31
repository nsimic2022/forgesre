from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import asset_type_abbrev, similar_incident_groups
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
    assert b"Add asset" in page.content
    assert b">Edit<" in page.content
    assert b">Clone<" in page.content
    assert b">Verify<" in page.content
    assert b">Remove<" in page.content
    assert b">#</" in page.content or b"Asset number" in page.content or b">#</th>" in page.content
    assert b'placeholder="search #, id, hostname, IP"' in page.content
    assert b'name="scrape_address"' in page.content
    assert b"?edit=" in page.content
    assert b"reach-dot" in page.content
    assert b">Ping<" in page.content
    assert b"Ping / comms" in page.content
    listed = client.get("/assets?edit=forge-demo-01")
    assert listed.status_code == 200
    assert b"Edit asset" in listed.content
    assert b'name="hostname"' in listed.content
    assert b'name="asset_id"' in listed.content
    assert b"readonly" in listed.content
    assert b"cannot change" in listed.content
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
    table, rest = page.text.split("</table>", 1)
    assert 'title="Linux Server">lnx</td>' in table
    assert 'title="Windows Server">win</td>' in table
    assert 'title="Network Switch">net</td>' in table
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


def test_asset_table_actions_cell_is_table_cell_not_flex_td():
    html = Path("frontend/templates/assets.html").read_text(encoding="utf-8")
    css = Path("frontend/static/app.css").read_text(encoding="utf-8")
    base = Path("frontend/templates/base.html").read_text(encoding="utf-8")
    table = html.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
    assert 'td class="row-actions"' not in table
    assert 'class="asset-actions"' in table
    assert 'class="row-actions"' in table
    assert "asset_type_abbrev" in table
    assert "vertical-align: middle" in css
    assert ".asset-table td.asset-actions" in css
    assert "display: table-cell" in css
    assert "td.row-actions" in css
    assert "app.css?v=asset-id-first-1" in base


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
    assert b"Ping / comms" in page.content
    assert b"reach-dot ping" in page.content
    assert b">Ping<" in page.content
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
    assert b'action="/assets/forge-demo-01/delete"' in page.content
    assert b">Remove<" in page.content
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
    assert b"reach-dot ping yellow" in listed.content
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
    body = page.content
    assert b"ICMP failed but exporter/SNMP" in body or b"ICMP unused/blocked" in body
    assert b"ICMP and port 22 are not required" in body
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
    listed = client.get("/assets")
    assert listed.status_code == 200
    detail = client.get(f"/assets/{host.asset_id}")
    assert detail.status_code == 200
    assert b"reach-dot ping yellow" in listed.content or b"reach-dot ping yellow" in detail.content
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
    assert str(host.number).encode() in listed.content
    found = client.get(f"/assets?q={host.number}")
    assert found.status_code == 200
    assert b"unique-num-host" in found.content
    api = client.get("/api/v1/assets").json()
    match = next(item for item in api if item["asset_id"] == "unique-num-host")
    assert match["number"] == host.number
    detail = client.get("/assets/unique-num-host")
    assert b"Asset number" in detail.content
    assert f"#{host.number}".encode() in detail.content

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
    add = client.get("/assets")
    assert add.status_code == 200
    id_at = add.text.find('name="asset_id"')
    host_at = add.text.find('name="hostname"')
    ip_at = add.text.find('name="ip"')
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
    assert created.headers.get("location", "").endswith("/assets/win10-gp")
    page = client.get("/assets/win10-gp")
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
    assert "Set at add; cannot change" in edit_form.text
    assert "readonly" in edit_form.text
    db.close()

