"""Discovery Found assets match Assets columns; Import vs Edit; token buttons; Asset ID slug."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import asset_id_slug, run_scan, upsert_candidate
from app.main import app
from app.models import Asset, DiscoveryCandidate
from app.seed import seed
from discovery import ALLOY_HTTP_PORT, SCAN_CUBE_META, SCAN_PORT_ORDER, ZABBIX_AGENT_PORT

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _ip() -> str:
    raw = uuid4().int
    return f"10.{200 + raw % 40}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def test_found_table_uses_assets_layout_and_scan_cubes():
    disc = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    assets = (ROOT / "frontend" / "templates" / "assets.html").read_text(encoding="utf-8")
    assert 'table class="asset-table"' in disc
    assert 'table class="asset-table"' in assets
    assert "found-table" not in disc
    assert "already in assets" in disc
    assert ">Import<" in disc
    assert "token-btn" in disc
    assert "zabbix.token_present" in disc and "glpi.token_present" in disc
    assert [item[0] for item in SCAN_CUBE_META] == list(SCAN_PORT_ORDER)
    assert SCAN_PORT_ORDER[-2:] == (ZABBIX_AGENT_PORT, ALLOY_HTTP_PORT)
    assert SCAN_CUBE_META[-2][1] == "10050"
    assert SCAN_CUBE_META[-1][1] == "12345"
    form = (ROOT / "frontend" / "templates" / "_asset_form.html").read_text(encoding="utf-8")
    row1 = form.split("asset-form-row-1", 1)[1].split("asset-form-row-2", 1)[0]
    row2 = form.split("asset-form-row-2", 1)[1]
    assert row1.index(">Identity<") < row1.index(">Ports<") < row1.index(">Metrics<") < row1.index(">Contacts<")
    assert ">Support<" not in row1
    assert row2.index(">Alarms<") < row2.index(">Escalation<") < row2.index(">Support<") < row2.index(">Notes<")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "bindAssetIdFromHostname" in js or "data-asset-hostname" in js
    assert "data-asset-hostname" in form
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".token-btn.is-yes" in css and ".token-btn.is-no" in css
    assert ".found-table { table-layout: fixed;" not in css


def test_scan_always_lists_known_ip_and_edit_prefills_saved_asset(monkeypatch):
    db = _db()
    ip = _ip()
    host = f"known-{uuid4().hex[:6]}"
    db.add(
        Asset(
            asset_id=host,
            hostname=host,
            ip=ip,
            type="Linux Server",
            environment="Production",
            status="healthy",
            owner="platform",
            contact_name="Ops desk",
            owner_email="ops@dc.local",
            source="manual",
            scrape_address=f"{ip}:9100",
        )
    )
    db.commit()
    monkeypatch.setattr(
        "discovery.probe_host",
        lambda target, **kwargs: {
            "ip": target,
            "open_ports": [22, 9100],
            "snmp_ok": False,
            "proposed_role": "Possible Linux server",
            "alive": True,
            "exporter_kind": "linux",
            "detect_message": "",
        },
    )
    result = run_scan(db, cidrs=[f"{ip}/32"])
    assert result["already"] == 1
    row = db.query(DiscoveryCandidate).filter_by(ip=ip).one()
    assert row.asset_id == host
    assert row.status == "approved"
    db.close()

    client = _login()
    listed = client.get("/discovery")
    assert listed.status_code == 200
    assert ip in listed.text
    assert "already in assets" in listed.text
    assert f"/discovery?asset={host}" in listed.text
    assert f">Import<" in listed.text or "Import" in listed.text
    opened = client.get(f"/discovery?asset={host}")
    assert opened.status_code == 200
    assert "Edit asset" in opened.text
    form = opened.text.split('id="asset-form"', 1)[1].split("</form>", 1)[0]
    assert f'value="{host}"' in form
    assert f'value="{ip}"' in form
    assert "ops@dc.local" in form
    assert "Ops desk" in form


def test_import_new_candidate_fills_add_asset_only_after_click():
    db = _db()
    ip = _ip()
    host = f"new-{uuid4().hex[:6]}"
    row = upsert_candidate(db, ip, "Possible Linux server", [22, 9100], node_exporter=True)
    row.hostname = host
    db.commit()
    row_id = row.id
    db.close()

    client = _login()
    blank = client.get("/discovery")
    assert blank.status_code == 200
    form = blank.text.split('id="asset-form"', 1)[1].split("</form>", 1)[0]
    assert f'value="{ip}"' not in form
    assert "Add asset" in blank.text
    assert f"/discovery?import={row_id}" in blank.text
    assert ">Approve<" not in blank.text
    opened = client.get(f"/discovery?import={row_id}")
    assert opened.status_code == 200
    assert "Add asset" in opened.text
    assert f"From scan {ip}" in opened.text
    filled = opened.text.split('id="asset-form"', 1)[1].split("</form>", 1)[0]
    assert f'value="{ip}"' in filled
    assert asset_id_slug(host) in filled or host in filled


def test_asset_id_slug_matches_python_helper():
    assert asset_id_slug("DESKTOP-CG81N3J") == "desktop-cg81n3j"
    assert asset_id_slug("Win 10 GP") == "win-10-gp"
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "[^a-zA-Z0-9-]" in js
    assert "data-asset-hostname" in js
    assert "locked" in js
