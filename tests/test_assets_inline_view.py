"""Assets list pane (View + glance + form) and Discovery Import into Add asset."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import candidate_form_values, candidate_inventory_guess
from app.main import app
from app.models import Asset, DiscoveryCandidate
from app.seed import DEMO_ASSET, seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_assets_list_has_no_empty_form_discovery_has_add_asset():
    _db().close()
    client = _client()
    assets = client.get("/assets")
    assert assets.status_code == 200
    assert 'id="asset-form"' not in assets.text
    assert "Add asset" not in assets.text
    assert 'action="/assets/open-view"' in assets.text
    listed = client.get(f"/assets?q={DEMO_ASSET}")
    assert listed.status_code == 200
    assert f'<a href="/assets/{DEMO_ASSET}" data-list-open>' not in listed.text
    assert f'<a href="/assets/{DEMO_ASSET}">{DEMO_ASSET}</a>' in listed.text
    assert f'/assets?view={DEMO_ASSET}' in listed.text
    discovery = client.get("/discovery")
    assert discovery.status_code == 200
    assert "Add asset" in discovery.text
    assert 'id="asset-form"' in discovery.text
    assert 'action="/discovery/open-import"' in discovery.text


def test_view_opens_glance_and_filled_form_on_the_list():
    _db().close()
    client = _client()
    page = client.get(f"/assets?view={DEMO_ASSET}")
    assert page.status_code == 200
    assert f'data-asset-glance="{DEMO_ASSET}"' in page.text
    for label in (">Collecting<", ">CPU<", ">RAM<", ">HDD<", ">NET<"):
        assert label in page.text.split("data-asset-glance", 1)[1].split("id=\"asset-form\"", 1)[0]
    assert "Edit asset" in page.text
    form = page.text.split('id="asset-form"', 1)[1]
    assert f'value="{DEMO_ASSET}"' in form
    assert 'name="hostname"' in form
    posted = client.post(
        "/assets/open-view",
        data={"selected": DEMO_ASSET, "stay": "/assets?q=forge"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert f"view={DEMO_ASSET}" in posted.headers["location"]
    assert "q=forge" in posted.headers["location"]
    saved = client.post(
        f"/assets/{DEMO_ASSET}/update",
        data={"hostname": "forge-demo-01", "ip": "10.20.30.40", "type": "Linux Server"},
        follow_redirects=False,
    )
    assert saved.status_code in {302, 303}
    assert f"view={DEMO_ASSET}" in saved.headers["location"]
    assert saved.headers["location"].startswith("/assets?")


def test_import_prefills_add_asset_and_save_creates():
    db = _db()
    token = uuid4().hex[:8]
    host = f"scan-{token}"
    ip = f"10.66.{int(token[:2], 16) % 200 + 1}.{int(token[2:4], 16) % 200 + 1}"
    row = DiscoveryCandidate(
        ip=ip,
        hostname=host,
        proposed_role="Possible Linux server",
        open_ports=[22, 9100],
        node_exporter=True,
        status="new",
        source="scan",
    )
    db.add(row)
    db.commit()
    row_id = row.id
    atype, _profile, scrape = candidate_inventory_guess(row)
    assert atype == "Linux Server"
    assert scrape == f"{ip}:9100"
    prefill = candidate_form_values(db, row)
    assert prefill["asset_id"] == host
    assert prefill["hostname"] == host
    assert prefill["ip"] == ip
    db.close()

    client = _client()
    opened = client.post(
        "/discovery/open-import",
        data={"selected": str(row_id), "stay": "/discovery"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert f"import={row_id}" in opened.headers["location"]
    page = client.get(opened.headers["location"])
    assert page.status_code == 200
    assert "Add asset" in page.text
    assert f"From scan {ip}" in page.text
    form = page.text.split('id="asset-form"', 1)[1]
    assert 'name="asset_id"' in form and f'value="{host}"' in form
    assert f'value="{ip}"' in form
    assert f'value="{ip}:9100"' in form
    created = client.post(
        "/assets",
        data={
            "asset_id": host,
            "hostname": host,
            "ip": ip,
            "type": "Linux Server",
            "import_of": str(row_id),
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    assert f"view={host}" in created.headers["location"]
    db = _db()
    asset = db.query(Asset).filter_by(asset_id=host).one()
    assert asset.ip == ip
    assert asset.source == "discovery"
    cand = db.get(DiscoveryCandidate, row_id)
    assert cand.status == "approved"
    assert cand.asset_id == host
    db.close()


def test_js_opens_view_on_enter_and_double_click():
    js = (__import__("pathlib").Path("frontend/static/app.js")).read_text(encoding="utf-8")
    assert "data-list-view" in js
    assert 'event.key === "Enter"' in js
    assert 'addEventListener("dblclick"' in js
    assert "openRow" in js
    css = (__import__("pathlib").Path("frontend/static/app.css")).read_text(encoding="utf-8")
    assert ".asset-glance" in css
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    pager = (__import__("pathlib").Path("frontend/templates/_pager.html")).read_text(encoding="utf-8")
    assert "open_view" in pager and ">View<" in pager
    assert "open_import" in pager and ">Import<" in pager
    assert pager.index("open_view") < pager.index("open_edit")
    assets = (__import__("pathlib").Path("frontend/templates/assets.html")).read_text(encoding="utf-8")
    assert 'include "_asset_form.html"' in assets.split("asset-pane", 1)[1]
    assert "include \"_asset_form.html\"" not in assets.split("<table", 1)[0]
    discovery = (__import__("pathlib").Path("frontend/templates/discovery.html")).read_text(encoding="utf-8")
    assert 'include "_asset_form.html"' in discovery.split("<h2>Found assets</h2>", 1)[1]
