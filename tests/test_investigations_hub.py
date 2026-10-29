"""Investigations hub: all cases on the menu page, one case from Dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.insights import list_investigation_runs
from app.main import app
from app.models import Incident
from app.seed import seed
from app.services import run_investigation

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _investigate() -> tuple[str, TestClient]:
    db = _db()
    incident = db.query(Incident).filter(Incident.asset_id.isnot(None)).first()
    assert incident is not None
    run_investigation(db, incident, actor="admin@forgesre.local", force=True, use_llm=False)
    number = incident.number
    db.close()
    return number, _client()


def test_investigations_page_opens_one_case_with_empty_cubes():
    number, client = _investigate()
    page = client.get(f"/investigations?n={number}")
    assert page.status_code == 200
    html = page.text
    assert 'id="activity"' in html
    assert 'id="summary"' in html
    assert ">Summary<" in html
    assert 'id="ai-more"' in html
    assert "case-specialists" in html
    assert "specialist-row-tall" in html
    assert number in html
    assert html.count("specialist-row-tall") == 1
    assert 'data-invest-list' not in html
    assert 'class="act-case-row' not in html.split('id="activity"', 1)[1]
    assert 'id="setup"' not in html
    assert "Ask ForgeAI" not in html
    listing = client.get("/investigations")
    assert listing.status_code == 200
    assert "case-specialists" in listing.text
    assert "specialist-row-tall" in listing.text
    assert ">Summary<" in listing.text
    assert number in listing.text
    assert client.get("/forgeai").status_code == 200


def test_dashboard_and_nav_open_investigations_hub():
    number, client = _investigate()
    home = client.get("/").text
    cube = home.split('id="forgeai"', 1)[1].split("</section>", 1)[0]
    assert "Recent investigations" in cube
    assert f'href="/investigations?n={number}"' in cube
    assert f'href="/ai/{number}"' not in cube
    nav = home.split('<aside class="nav"', 1)[1].split("</aside>", 1)[0]
    assert ">Investigations<" in nav
    assert 'href="/investigations"' in nav
    assert "data-nav-invest" not in nav
    assert "data-nav-invest-toggle" not in nav
    assert "nav-invest-panel" not in nav
    assert client.get("/investigations/menu").status_code == 404
    incident = client.get(f"/incidents/{number}").text
    assert f'href="/ai/{number}"' in incident
    db = _db()
    runs = list_investigation_runs(db)
    match = next(row for row in runs if row["number"] == number)
    assert match["href"] == f"/investigations?n={number}"
    db.close()


def test_nav_is_plain_link_and_cache():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "app.css?v=v09-64" in base
    assert "app.js?v=v09-S" in base
    assert 'href="/investigations"' in base
    assert "data-nav-invest-toggle" not in base
    assert ".nav-invest-panel {" not in css
    assert ".invest-stack {" in css
    assert ".invest-empty {" in css
    assert "bindNavInvest" not in js
    assert "/investigations/menu" not in js
