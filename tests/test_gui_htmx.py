"""HTMX + Alpine trial: vendored offline, boost nav, downloads stay native."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Incident
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
LOGIN = (ROOT / "frontend" / "templates" / "login.html").read_text(encoding="utf-8")
HTMX = ROOT / "frontend" / "static" / "vendor" / "htmx.min.js"
ALPINE = ROOT / "frontend" / "static" / "vendor" / "alpine.min.js"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_vendor_htmx_and_alpine_are_offline():
    assert HTMX.is_file() and HTMX.stat().st_size > 20_000
    assert ALPINE.is_file() and ALPINE.stat().st_size > 20_000
    assert "2.0.4" in HTMX.read_text(encoding="utf-8", errors="ignore")
    assert "3.14.8" in ALPINE.read_text(encoding="utf-8", errors="ignore")
    assert "/static/vendor/htmx.min.js" in BASE
    assert "/static/vendor/alpine.min.js" in BASE
    assert "cdn.jsdelivr" not in BASE
    assert "unpkg.com" not in BASE


def test_cache_bust_and_boost_chrome():
    assert "app.css?v=v09-62" in BASE
    assert "app.js?v=v09-S" in BASE
    assert 'hx-boost="true"' in BASE
    assert 'hx-indicator="#hx-progress"' in BASE
    assert 'id="hx-progress"' in BASE
    assert 'hx-boost="false"' in BASE
    assert "/static/vendor/htmx.min.js" in BASE.split("<head>", 1)[1].split("</head>", 1)[0]
    assert "/static/app.js?v=v09-S" in BASE.split("<head>", 1)[1].split("</head>", 1)[0]


def test_downloads_and_multipart_are_not_boosted():
    pager = (ROOT / "frontend" / "templates" / "_pager.html").read_text(encoding="utf-8")
    incident = (ROOT / "frontend" / "templates" / "incident_detail.html").read_text(encoding="utf-8")
    admin = (ROOT / "frontend" / "templates" / "admin.html").read_text(encoding="utf-8")
    asset = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    assert 'data-export-picker hx-boost="false"' in pager
    assert 'data-export-from-report hx-boost="false"' in incident
    assert 'href="/admin/backups/{{ b.name }}" hx-boost="false"' in admin
    assert 'enctype="multipart/form-data"' in admin
    assert 'action="/admin/backups/import"' in admin
    assert admin.split('action="/admin/backups/import"', 1)[1].split(">", 1)[0].count('hx-boost="false"')
    assert 'action="/assets/export" hx-boost="false"' in asset
    assert 'action="/logout" hx-boost="false"' in BASE


def test_js_is_boost_safe():
    assert "function bootUi()" in JS
    assert "function bindOnce(" in JS
    assert "htmx:afterSettle" in JS
    assert "Alpine.initTree" in JS
    assert "ForgeSRE.every" in JS
    assert "ForgeSRE.stop" in JS
    clock = JS.split("(function bindClock()", 1)[1].split("})();", 1)[0]
    assert 'document.querySelectorAll("[data-clock]")' in clock
    assert clock.index("const tick") < clock.index("querySelectorAll")
    assert 'data-row-select-bound' in JS
    assert 'data-export-bound' in JS
    assert 'data-tips-bound' in JS
    assert 'data-onoff-bound' in JS
    assert "barOf" in JS
    assert 'querySelector("[data-appliance-resources]")' in JS
    assert 'form.setAttribute("hx-boost", "false")' in JS


def test_design_tokens_do_not_replace_locked_hexes():
    assert "--radius-sm: 8px;" in CSS
    assert "--shadow:" in CSS
    assert "[x-cloak]" in CSS
    assert ".hx-progress" in CSS
    assert ".password-toggle" in CSS
    light = CSS.split('html[data-theme="light"],', 1)[1].split("}", 1)[0]
    dark = CSS.split('html[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    assert "--bg: #f9fafb;" in light
    assert "--text: #0f172a;" in light
    assert "--ok: #059669;" in light
    assert "--bg: #0f172a;" in dark
    assert "--text: #f8fafc;" in dark
    assert "--ok: #34d399;" in dark
    big = CSS.split(".stat-row-big .stat {", 1)[1].split("}", 1)[0]
    assert "aspect-ratio: 1" in big
    assert "@keyframes heat-pulse-crit" in CSS
    assert ".stack-cube.ok { background: var(--ok)" in CSS


def test_login_uses_alpine_password_toggle():
    assert 'x-data="{ show: false }"' in LOGIN
    assert 'name="password"' in LOGIN
    assert 'type="password"' in LOGIN
    assert "@click=" in LOGIN


def test_pages_serve_htmx_and_vendor_assets():
    db = _db()
    client = TestClient(app)
    login = client.get("/login")
    assert login.status_code == 200
    assert 'hx-boost="true"' in login.text
    assert "/static/vendor/htmx.min.js" in login.text
    assert "/static/vendor/alpine.min.js" in login.text
    assert 'name="password"' in login.text
    htmx = client.get("/static/vendor/htmx.min.js")
    alpine = client.get("/static/vendor/alpine.min.js")
    assert htmx.status_code == 200
    assert alpine.status_code == 200
    assert "2.0.4" in htmx.text
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200
    assert 'hx-boost="true"' in home.text
    assert 'id="hx-progress"' in home.text
    assert "/static/vendor/htmx.min.js" in home.text
    db.close()


def test_every_template_form_opts_out_of_boost():
    for path in (ROOT / "frontend" / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<form\b[^>]*>", text):
            assert 'hx-boost="false"' in match.group(0), f"{path.name}: {match.group(0)}"
    assert 'querySelectorAll("form")' in BASE.split("</body>", 1)[0]


def test_incident_lists_open_the_incident_page():
    dash = (ROOT / "frontend" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    incidents = (ROOT / "frontend" / "templates" / "incidents.html").read_text(encoding="utf-8")
    assert 'data-list-view="/incidents/{{ item.number }}"' in dash
    assert 'data-list-view="/incidents/{{ item.number }}"' in incidents
    assert "On-call" in dash
    assert "Roster" not in dash
    assert "metric-toolbar" not in dash
    assert "data-dash-graph-host" not in dash
    assert "dash-graphs-spacer" in dash
    assert dash.index("dash-graphs-spacer") < dash.index("data-dash-graphs")
    assert "repeat(2, minmax(0, 1fr))" not in CSS.split(".dash-graphs .dash-graph-list {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr)" in CSS.split(".dash-graphs .dash-graph-list {", 1)[1]
    assert "height: 9.25rem" in CSS
    top = CSS.split(".dash-top {", 1)[1].split("}", 1)[0]
    assert "minmax(0, var(--dash-tile-band))" in top
    assert "minmax(24rem, 2.6fr)" in top
    band = CSS.split(".dash-incidents {", 1)[1].split("}", 1)[0]
    assert "minmax(0, 2fr) minmax(0, 1fr)" in band
    graphs = JS.split("(function bindDashGraphs() {", 1)[1].split("\n})();", 1)[0]
    assert 'subject.textContent = number + (title ? " · " + title : "")' in graphs
    assert 'hostEl.textContent = host || asset || ""' in graphs


def test_edit_one_incident_opens_clean_detail_url():
    db = _db()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200
    assert 'hx-boost="false"' in home.text
    assert "On-call" in home.text
    demo = client.get("/?demo=1")
    listed = client.get("/incidents")
    assert listed.status_code == 200
    assert 'data-list-view="/incidents/' in listed.text
    assert 'data-list-view="/incidents/' in demo.text or 'data-list-view="/incidents/' in home.text
    row = db.query(Incident).first()
    assert row is not None
    opened = client.post(
        "/incidents/edit",
        data={"selected": row.number, "next": "/"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert opened.headers["location"] == f"/incidents/{row.number}"
    detail = client.get(opened.headers["location"])
    assert detail.status_code == 200
    assert row.number in detail.text
    db.close()
