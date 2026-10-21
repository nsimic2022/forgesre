"""GUI contextual ⓘ help: one partial, used on operator pages."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"
_TIP_BLOCK = re.compile(r"\{%\s*set tip\s*%\}.*?\{%\s*endset\s*%\}", re.S)


def _outside_tips(source: str) -> str:
    """Template text that is not inside an info-tip copy block."""
    return _TIP_BLOCK.sub("", source)


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
    assert "app.css?v=v09-26" in base


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


def test_title_subtitles_live_only_in_info_tips():
    """Help under a page title is one hover tip. The old lede is not body text."""
    rules = (TEMPLATES / "playrules.html").read_text(encoding="utf-8")
    rules_h1 = rules.split("</h1>", 1)[0]
    assert rules_h1.count('include "_info_tip.html"') == 1
    for phrase in (
        "A rule fires nothing",
        "matches its alertname",
        "cannot fire earlier than the Prometheus rule",
        "do not create Prom rules",
        "assets.alarms",
        "not a second alerting engine",
    ):
        assert phrase in rules_h1, phrase
        assert phrase not in _outside_tips(rules), phrase
    assert 'class="page-lede"' not in rules

    books = (TEMPLATES / "playbooks.html").read_text(encoding="utf-8")
    books_h1 = books.split("</h1>", 1)[0]
    assert books_h1.count('include "_info_tip.html"') == 1
    assert "A playbook is a checklist" in books_h1
    assert "does not tick them off" in books_h1
    assert "does not execute commands on the host" in books_h1
    assert "A playbook is a checklist" not in _outside_tips(books)
    assert "slug must be unique" in books
    assert "slug must be unique" not in _outside_tips(books)

    esc = (TEMPLATES / "escalation.html").read_text(encoding="utf-8")
    esc_h1 = esc.split("</h1>", 1)[0]
    assert esc_h1.count('include "_info_tip.html"') == 1
    assert "Each level is minutes after the incident opened" in esc_h1
    assert "does not invent" in esc_h1
    assert "Acknowledge stops further steps" in esc_h1
    assert "does not invent" not in _outside_tips(esc)
    assert "recipient-rules" not in esc
    assert "/ops#mail" in esc
    assert "/ops#mail" not in _outside_tips(esc)

    health = (TEMPLATES / "health.html").read_text(encoding="utf-8")
    assert "Same live checks as ./forgesre doctor" in health.split("</h1>", 1)[0]
    assert "Same live checks" not in _outside_tips(health)
    assert "Overall:" in _outside_tips(health)

    dash = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    dash_h1 = dash.split("</h1>", 1)[0]
    assert dash_h1.count('include "_info_tip.html"') == 1
    assert "Full stack diagnostics live on System Health" in dash_h1
    assert "The journal is on System Health" in dash_h1
    assert "the internal process log, not Loki" in dash_h1
    assert "Full stack diagnostics live on" not in _outside_tips(dash)
    assert "The journal is on System Health" not in _outside_tips(dash)
    assert '<p class="muted">Full stack diagnostics' not in dash
    assert "Raises the demo CPU gauge to 94%" in _outside_tips(dash)
    assert 'include "_info_tip.html"' not in dash.split('id="demo-panel"', 1)[1].split("</dialog>", 1)[0]
    assert "Reset demo gauges" not in dash
    assert "Not a live SNMP walk" in _outside_tips(dash)

    form = (TEMPLATES / "_asset_form.html").read_text(encoding="utf-8")
    assert "It cannot fire earlier than the Prometheus rule in alerts.yml." not in _outside_tips(form)
    assert "This does not create Prometheus thresholds." not in _outside_tips(form)
    assert "Copy of {{ clone_source }}" not in _outside_tips(form)
    assert 'class="alarm-limit"' not in form

    detail = (TEMPLATES / "asset_detail.html").read_text(encoding="utf-8")
    assert "Does not create Prometheus thresholds" not in _outside_tips(detail)

    incident = (TEMPLATES / "incident_detail.html").read_text(encoding="utf-8")
    assert "Guidance only" not in _outside_tips(incident)
    assert "Matched playrule" in _outside_tips(incident)

    admin = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    visible_admin = _outside_tips(admin).lower()
    assert "no terminal in this browser" not in visible_admin
    assert "restore overwrites the live database" not in visible_admin
    assert "type a new email and password" not in visible_admin

    ops = (TEMPLATES / "ops.html").read_text(encoding="utf-8")
    visible_ops = _outside_tips(ops)
    assert "That is not Remove." in ops
    assert "<strong>Enabled</strong> means the job fires" not in visible_ops
    assert "Same fields as create" not in visible_ops

    verify = (TEMPLATES / "assets_verify.html").read_text(encoding="utf-8")
    one = (TEMPLATES / "asset_verify.html").read_text(encoding="utf-8")
    assert "not ./forgesre test" not in _outside_tips(verify)
    assert "not ./forgesre doctor" not in _outside_tips(one)

    for path in TEMPLATES.glob("*.html"):
        assert 'class="page-lede"' not in path.read_text(encoding="utf-8"), path.name


def test_rules_playbooks_and_escalation_render_one_title_tip():
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

    rules = client.get("/playrules")
    assert rules.status_code == 200
    h1 = rules.text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    after = rules.text.split("</h1>", 1)[1]
    assert h1.count('class="info-tip"') == 1
    assert "A rule fires nothing" in h1
    assert "cannot fire earlier than the Prometheus rule" in h1
    assert 'class="page-lede"' not in rules.text
    assert "<p class=\"page-lede\">" not in rules.text
    assert "A rule fires nothing" not in after.split("<h2", 1)[0]

    books = client.get("/playbooks")
    assert books.status_code == 200
    book_h1 = books.text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    book_after = books.text.split("</h1>", 1)[1]
    assert book_h1.count('class="info-tip"') == 1
    assert "checklist shown on the incident page" in book_h1
    assert "checklist shown on the incident page" not in book_after.split("<h2", 1)[0]

    esc = client.get("/escalation")
    assert esc.status_code == 200
    esc_h1 = esc.text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    esc_after = esc.text.split("</h1>", 1)[1]
    assert esc_h1.count('class="info-tip"') == 1
    assert "does not invent" in esc_h1
    assert "minutes after the incident opened" in esc_h1
    assert "does not invent" not in esc_after
    assert "recipient-rules" not in esc.text
    assert "/ops#mail" in esc.text

    home = client.get("/")
    assert home.status_code == 200
    home_h1 = home.text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    assert home_h1.count('class="info-tip"') == 1
    assert "Full stack diagnostics live on System Health" in home_h1
    assert "The journal is on System Health" in home_h1
    assert "the internal process log, not Loki" in home_h1
    under_cubes = home.text.split("data-appliance-card", 1)[1].split("</aside>", 1)[1].split("data-dash-incidents", 1)[0]
    assert "Full stack diagnostics" not in under_cubes
    assert "The journal is on" not in under_cubes
    assert "<p" not in under_cubes
