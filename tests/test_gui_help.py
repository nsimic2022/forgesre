"""GUI contextual ⓘ help: one partial, used on operator pages."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


def test_info_tip_partial_and_css_exist():
    partial = (TEMPLATES / "_info_tip.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'class="info-tip"' in partial
    assert "info-tip-icon" in partial
    assert "aria-label" in partial
    assert "title=" in partial
    assert ".info-tip" in css
    assert ".info-tip-bubble" in css
    assert "bindInfoTips" in js
    assert "app.css?v=help-1" in base


def test_discovery_and_assets_include_info_tip():
    discovery = (TEMPLATES / "discovery.html").read_text(encoding="utf-8")
    assets = (TEMPLATES / "assets.html").read_text(encoding="utf-8")
    form = (TEMPLATES / "_asset_form.html").read_text(encoding="utf-8")
    assert 'include "_info_tip.html"' in discovery
    assert 'include "_info_tip.html"' in assets
    assert 'include "_info_tip.html"' in form
    assert "not nmap" in discovery.lower()
    assert "Set at add; cannot change" in form


def test_assets_and_discovery_pages_render_info_tip():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    assets = client.get("/assets")
    discovery = client.get("/discovery")
    assert assets.status_code == 200
    assert discovery.status_code == 200
    assert 'class="info-tip"' in assets.text
    assert 'class="info-tip"' in discovery.text
    assert "./forgesre test" in assets.text
    assert "./forgesre doctor" in assets.text
    assert "not nmap" in discovery.text.lower()
    assert "TCP/161 is skipped" in discovery.text
