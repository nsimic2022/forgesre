"""Investigations hub: one open case, nav flyout, Dashboard links, empty cubes."""

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
    assert 'data-invest-list' not in html
    assert 'class="act-case-row' not in html.split('id="activity"', 1)[1]
    assert 'id="setup"' not in html
    assert 'id="ask"' not in html
    assert 'id="digest"' not in html
    assert 'id="jobs"' not in html
    assert 'id="reports"' not in html
    assert 'id="cron"' not in html
    assert "Ask ForgeAI" not in html
    assert "Save digest schedule" not in html
    assert "Recent activity" not in html
    home = client.get("/investigations")
    assert home.status_code == 200
    assert "case-specialists" in home.text
    assert client.get("/forgeai").status_code == 200


def test_dashboard_and_nav_open_investigations_hub():
    number, client = _investigate()
    home = client.get("/").text
    cube = home.split('id="forgeai"', 1)[1].split("</section>", 1)[0]
    assert "Recent investigations" in cube
    assert f'href="/investigations?n={number}"' in cube
    assert f'href="/ai/{number}"' not in cube
    assert 'name="selected"' not in cube
    nav = home.split('<aside class="nav"', 1)[1].split("</aside>", 1)[0]
    assert 'data-nav-invest' in nav
    assert 'data-nav-invest-toggle' in nav
    assert ">Investigations<" in nav
    assert 'href="/investigations"' in nav
    assert "Recent investigations" in nav
    assert 'data-nav-invest-list' in nav
    assert 'name="selected"' not in nav
    menu = client.get("/investigations/menu")
    assert menu.status_code == 200
    assert 'name="selected"' in menu.text
    assert f'href="/investigations?n={number}"' in menu.text
    assert menu.text.count('name="selected"') <= 10
    assert ">History<" in menu.text
    incident = client.get(f"/incidents/{number}").text
    assert f'href="/ai/{number}"' in incident
    db = _db()
    runs = list_investigation_runs(db)
    match = next(row for row in runs if row["number"] == number)
    assert match["href"] == f"/investigations?n={number}"
    assert match["specialists"][0].get("facts") is not None
    db.close()


def test_nav_flyout_css_and_cache():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "app.css?v=v09-45" in base
    assert "app.js?v=v09-H" in base
    assert ".nav-invest-panel {" in css
    assert "left: 228px" in css.split(".nav-invest-panel {", 1)[1].split("}", 1)[0]
    assert ".invest-empty {" in css
    assert ".invest-case {" in css
    assert "max-height: 28rem" in css.split(".specialist-row-tall .specialist-cube {", 1)[1].split("}", 1)[0]
    assert "bindNavInvest" in js
    assert "data-nav-invest-toggle" in js
    assert '"228px"' in js
    assert "/investigations/menu" in js
