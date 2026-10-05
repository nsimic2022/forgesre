"""Dashboard layout pass: banner/section order, big tiles, appliance card, incidents heat, per-page select, row checkboxes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import or_

from app.db import Base, SessionLocal, engine
from app.history import (
    PAGE_SIZE,
    PAGE_SIZE_CHOICES,
    dashboard_incident_tiles,
    incident_heat,
    paginate,
    pager_state,
    parse_per_page,
    per_page_param,
)
from app.journal import report
from app.main import app
from app.models import AuditLog, DiscoveryCandidate, Incident, JournalEntry, Notification, ScheduledReport
from app.seed import seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _drop_layout_rows():
    """Shared sqlite DB: later tests assume a small incident count (next_incident_number), so remove what we add."""
    yield
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(Incident).filter(
        or_(Incident.fingerprint.like("layout:%"), Incident.fingerprint.like("NodeExporterDown:layout-%"))
    ).delete(synchronize_session=False)
    db.query(Notification).filter(Notification.step_key == "layout").delete(synchronize_session=False)
    db.query(ScheduledReport).filter(ScheduledReport.name.like("layout-%")).delete(synchronize_session=False)
    db.query(DiscoveryCandidate).filter(DiscoveryCandidate.ip.like("10.66.%")).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.action == "layout.test").delete(synchronize_session=False)
    db.query(JournalEntry).filter(JournalEntry.action == "layout").delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add_incidents(db, n: int, *, severity: str = "WARNING", status: str = "OPEN") -> str:
    token = uuid4().hex[:8]
    for i in range(n):
        db.add(
            Incident(
                number=next_incident_number(db),
                title=f"Layout {token} {i:03d}",
                severity=severity,
                status=status,
                fingerprint=f"layout:{token}:{i}",
                started_at=datetime.now(timezone.utc),
                summary="layout fixture",
            )
        )
        db.flush()
    db.commit()
    return token


def _tbody(html: str) -> str:
    return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def _rows(html: str) -> int:
    return _tbody(html).count("<tr")


def test_parse_per_page_default_choices_and_junk():
    assert PAGE_SIZE == 10
    assert PAGE_SIZE_CHOICES == (10, 20, 50, 100)
    assert parse_per_page(None) == 10
    assert parse_per_page("") == 10
    for n in PAGE_SIZE_CHOICES:
        assert parse_per_page(str(n)) == n
        assert parse_per_page(n) == n
    assert parse_per_page(" 50 ") == 50
    assert parse_per_page("abc") == 10
    assert parse_per_page("20; DROP TABLE") == 10
    assert parse_per_page("1e3") == 10
    assert parse_per_page("0") == 10
    assert parse_per_page("-20") == 10
    assert parse_per_page("15") == 20
    assert parse_per_page("1000") == 100
    assert parse_per_page("101") == 100


def test_per_page_param_pairs_with_page_key():
    assert per_page_param("page") == "per_page"
    assert per_page_param("reports_page") == "reports_per_page"
    assert per_page_param("audit_page") == "audit_per_page"
    assert per_page_param("backup_page") == "backup_per_page"


def test_paginate_honors_size_and_clamps():
    rows = list(range(250))
    ten, state = paginate(rows, "1")
    assert len(ten) == 10 and state["size"] == 10 and state["size_param"] == "per_page"
    twenty, state = paginate(rows, "2", size="20")
    assert twenty == list(range(20, 40))
    assert state["pages"] == 13
    capped, state = paginate(rows, "1", size="5000")
    assert len(capped) == 100 and state["size"] == 100
    junk, state = paginate(rows, "1", size="nope")
    assert len(junk) == 10
    assert pager_state("1", total=10)["show_size"] is False
    assert pager_state("1", total=11)["show_size"] is True


def test_incidents_per_page_query_param():
    db = _db()
    _add_incidents(db, 60)
    client = _client()
    default = client.get("/incidents")
    assert default.status_code == 200
    assert _rows(default.text) == 10
    twenty = client.get("/incidents?per_page=20")
    assert _rows(twenty.text) == 20
    assert '<option value="20" selected>' in twenty.text
    assert 'name="per_page"' in twenty.text
    assert "per_page=20" in twenty.text.split('class="pager"', 1)[1]
    filters = twenty.text.split('class="list-filters incidents-filters"', 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="per_page" value="20">' in filters
    assert _rows(client.get("/incidents?per_page=junk").text) == 10
    assert _rows(client.get("/incidents?per_page=0").text) == 10
    big = client.get("/incidents?per_page=1000")
    assert 60 <= _rows(big.text) <= 100
    assert '<option value="100" selected>' in big.text
    page_two = client.get("/incidents?per_page=20&page=2")
    assert _rows(page_two.text) == 20
    db.close()


def test_pager_select_bottom_right_on_every_listed_surface():
    db = _db()
    _add_incidents(db, 12)
    token = uuid4().hex[:8]
    for i in range(25):
        report(db, "jobs", "layout", "ok", summary=f"Layout journal {token} {i}")
        db.add(Notification(target=f"l{i}@example.local", subject=f"Layout {token} {i}", body="x", status="generated", step_key="layout"))
        db.add(ScheduledReport(name=f"layout-{token}-{i}", to_email="ops@example.local", interval_hours=6))
        db.add(DiscoveryCandidate(ip=f"10.66.{i}.{(int(token[:2], 16) % 200) + 1}", proposed_role="Unknown device", status="new", source="scan"))
        db.add(AuditLog(action="layout.test", actor="tester", object_type="layout", object_id=str(i)))
    db.commit()
    client = _client()
    for path, key in [
        ("/", "per_page"),
        ("/incidents", "per_page"),
        ("/history", "per_page"),
        ("/discovery", "per_page"),
        ("/journal", "per_page"),
        ("/admin", "audit_per_page"),
    ]:
        page = client.get(path)
        assert page.status_code == 200, path
        assert 'class="pager-bar"' in page.text, path
        assert f'name="{key}"' in page.text, path
        assert "data-pager-size" in page.text, path
    ops = client.get("/ops")
    mail = ops.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    reports = ops.text.split('id="reports"', 1)[1]
    assert 'name="per_page"' in mail and 'action="/ops#mail"' in mail
    assert 'name="reports_per_page"' in reports and 'action="/ops#reports"' in reports
    split = client.get("/ops?reports_per_page=20")
    mail = split.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    reports = split.text.split('id="reports"', 1)[1]
    assert _rows(mail) == 10
    assert _rows(reports) == 20
    assert "<th>When</th><th>To</th><th>Subject</th><th>Status</th><th>Body</th>" in mail
    audit = client.get("/admin?audit_per_page=20")
    table = audit.text.split("<h2>Audit log</h2>", 1)[1]
    assert _rows(table) == 20
    db.close()


def test_row_checkboxes_named_selected_on_list_surfaces():
    db = _db()
    _add_incidents(db, 2)
    db.add(Notification(target="sel@example.local", subject="Layout select", body="x", status="generated", step_key="layout"))
    db.add(ScheduledReport(name=f"layout-sel-{uuid4().hex[:6]}", to_email="ops@example.local", interval_hours=6))
    db.commit()
    client = _client()
    for path in ["/", "/incidents", "/history", "/assets", "/discovery", "/playrules", "/playbooks", "/ops", "/admin"]:
        page = client.get(path)
        assert page.status_code == 200, path
        assert 'name="selected"' in page.text, path
        assert "data-select-page" in page.text, path
        assert "data-select-row" in page.text, path
    incidents = client.get("/incidents").text
    head = incidents.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert head.index("data-select-page") < head.index(">Incident<")
    assert "delete-selected" not in incidents
    admin = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    backup_table = admin.split("{% for b in backups %}", 1)[1].split("</tr>", 1)[0]
    assert "sel.cell(b.name)" in backup_table
    assert 'name="selected"' in (TEMPLATES / "_select.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "bindRowSelect" in js and "bindPagerSize" in js


def test_dashboard_order_banners_tiles_appliance_sections():
    db = _db()
    db.add(
        Incident(
            number=next_incident_number(db),
            title="Layout host down",
            severity="CRITICAL",
            status="OPEN",
            fingerprint=f"NodeExporterDown:layout-{uuid4().hex[:6]}",
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    report(db, "jobs", "layout", "error", summary="Layout journal error")
    if not db.query(DiscoveryCandidate).filter_by(status="new").count():
        db.add(DiscoveryCandidate(ip="10.66.250.9", proposed_role="Unknown device", status="new", source="scan"))
        db.commit()
    html = _client().get("/").text
    host = html.index('id="host-down-banner"')
    device = html.index("NEW DEVICE DETECTED")
    journal = html.index('id="journal-error-banner"')
    top = html.index('class="dash-top"')
    assert host < device < journal < top
    incidents_tile = html.index("data-incident-tile")
    infra = html.index("dash-tiles-infra")
    appliance = html.index("data-appliance-card")
    recent = html.index("<h2>Recent incidents</h2>")
    journal_list = html.index("Recent journal reports")
    assert top < incidents_tile < infra < appliance < recent < journal_list
    assert html.count("stat-row-big") == 2
    card = html[appliance : html.index("</aside>", appliance)]
    assert "data-clock" in card
    for key in ("cpu", "ram", "hdd"):
        assert f'data-metric="{key}"' in card
    assert "metric-dash" in card
    assert "not an inventory asset" in card
    assert "80%" in card and "95%" in card
    db.close()


def test_incident_heat_levels():
    def tiles(**counts):
        return [{"key": key, "count": counts.get(key, 0)} for key in ("open", "critical", "investigating", "escalated", "resolved")]

    assert incident_heat(tiles()) == ""
    assert incident_heat(tiles(resolved=4)) == ""
    assert incident_heat(tiles(open=1)) == "warn"
    assert incident_heat(tiles(investigating=2, resolved=1)) == "warn"
    assert incident_heat(tiles(escalated=1)) == "warn"
    assert incident_heat(tiles(open=1, critical=1)) == "crit"


def test_dashboard_heat_class_only_on_incidents_tile():
    db = _db()
    _add_incidents(db, 1, severity="CRITICAL")
    assert incident_heat(dashboard_incident_tiles(db)) == "crit"
    html = _client().get("/").text
    match = re.search(r'<div class="tile-group([^"]*)" data-incident-tile data-incident-heat="([^"]+)"', html)
    assert match is not None
    assert match.group(1).strip() == "heat-crit"
    assert match.group(2) == "crit"
    assert html.count("heat-crit") == 1
    assert "heat-" not in html.split("dash-tiles-infra", 1)[1].split("</section>", 1)[0]
    assert "heat-" not in _tbody(html.split("<h2>Recent incidents</h2>", 1)[1])
    template = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert "heat-{{ incident_heat }}" in template
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "@keyframes heat-pulse-crit" in css
    assert "@keyframes heat-pulse-warn" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("}\n}", 1)[0]
    assert "heat-crit::before" in reduced and "animation: none" in reduced
    db.close()


def test_dashboard_tiles_bigger_and_thick_fill():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    big = css.split(".stat-row-big .stat {", 1)[1].split("}", 1)[0]
    assert "border-left-width: 8px" in big
    assert "min-height: 7.4rem" in big
    assert "font-size: 2.8rem" in css
    assert ".stat-row-big .stat.crit { background: var(--pill-crit-bg)" in css
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=dash-layout-1" in base
