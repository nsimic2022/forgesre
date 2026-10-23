"""Dashboard Recent investigations, ticker once, Assets/Found headers, Discovery cubes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def _client() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_dashboard_recent_investigations_scrolls_and_host_metrics_spacer():
    dash = (ROOT / "frontend" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    cube = dash.split('id="forgeai"', 1)[1].split("</section>", 1)[0]
    assert "Recent investigations" in cube
    assert "Recent activity" not in cube
    assert "dash-ai-scroll" in cube
    assert "metric-toolbar" not in dash
    assert "Open asset" not in dash
    assert "data-dash-graph-subject" not in dash
    assert "dash-graphs-spacer" in dash
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    scroll = css.split(".dash-ai-scroll {", 1)[1].split("}", 1)[0]
    assert "min-height: 0" in scroll
    assert "overflow: auto" in scroll
    cube_css = css.split(".dash-ai-cube {", 2)[2].split("}", 1)[0]
    assert "overflow: hidden" in cube_css
    assert "height: 100%" in cube_css
    spacer = css.split(".dash-graphs-spacer {", 1)[1].split("}", 1)[0]
    assert "2.6em" in spacer
    html = _client().get("/").text
    home_cube = html.split('id="forgeai"', 1)[1].split("</section>", 1)[0]
    assert "Recent investigations" in home_cube
    assert "dash-graphs-spacer" in html
    assert "metric-toolbar" not in html


def test_ticker_shows_once_then_hides_and_click_dismisses():
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    ticker = js.split("(function bindAlarmTicker() {", 1)[1].split("\n})();", 1)[0]
    assert 'SEEN_KEY = "forgesre-ticker-seen"' in ticker
    assert "sessionStorage" in ticker
    assert 'path === "/"' in ticker
    assert "markSeen" in ticker
    assert "ticker-blink" in ticker
    assert 'closest("[data-ticker-link]")' in ticker
    assert "bindOnce" in ticker


def test_inventory_headers_have_no_column_infoboxes():
    head = (ROOT / "frontend" / "templates" / "_inventory_head.html").read_text(encoding="utf-8")
    assert 'include "_info_tip.html"' not in head
    assert "label-line" not in head
    for name in (">ID<", ">Hostname<", ">IP<", ">Port<", ">Type<", ">Status<", ">L1–L4<", ">Notes<"):
        assert name in head
    client = _client()
    assets = client.get("/assets").text.split('<table class="asset-table"', 1)[1].split("</thead>", 1)[0]
    found = client.get("/discovery").text.split("<h2>Found assets</h2>", 1)[1].split("</thead>", 1)[0]
    for block in (assets, found):
        assert 'class="info-tip"' not in block
        assert ">ID<" in block and ">Port<" in block and ">Type<" in block and ">Notes<" in block


def test_discovery_cubes_match_and_found_columns_stay_even():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    actions = css.split(".discovery-actions {", 1)[1].split("}", 1)[0]
    assert "align-items: stretch" in actions
    assert "repeat(4, minmax(0, 1fr))" in actions
    cards = css.split(".discovery-actions .card {", 1)[1].split("}", 1)[0]
    assert "height: 100%" in cards
    assert "min-height: 12.5rem" in cards
    assert ".found-table { table-layout: fixed;" in css
    row2 = css.split(".asset-form-row-2 {", 1)[1].split("}", 1)[0]
    assert "repeat(4, minmax(0, 1fr))" in row2
    assert "grid-auto-rows: 28rem" in row2
    assert "overflow-y: auto" in css.split(".asset-custom-scroll", 1)[1].split("}", 1)[0]
    assert "policy-ladder" in css
    form = (ROOT / "frontend" / "templates" / "_asset_form.html").read_text(encoding="utf-8")
    row = form.split("asset-form-row-2", 1)[1]
    assert "asset-alarms-cube" in row
    assert "alarm-standard-grid" in row
    assert "asset-custom-scroll" in row
    assert "asset-form-row2-empty" in row
    assert "asset-form-row-3" not in form
    assert row.index("Standard alarms") < row.index("Custom alarms") < row.index(">Escalation<") < row.index(">Notes<")
    assert 'class="card asset-form-block asset-form-cube asset-alarms-cube"' in row
    assert 'class="card asset-form-block asset-form-cube asset-ladder"' in row
    assert "policy-ladder" in row
    assert "ladder_target_" in row
    assert "for humans" not in row
    html = _client().get("/discovery").text
    assert "asset-form-row2-empty" in html
    assert "asset-alarms-cube" in html
    assert "discovery-actions" in html
