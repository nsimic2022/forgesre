"""Scan-friendly list language on Dashboard and Incidents."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed
from app.services import (
    format_started_at,
    incident_seq,
    incident_short_label,
    incident_when_label,
    severity_pill,
    short_when_label,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient) -> None:
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )


def _headers(html: str) -> str:
    return html.split("<thead>", 1)[1].split("</thead>", 1)[0]


def test_list_helpers_short_id_when_and_severity():
    from app.services import _appliance_local

    now = datetime(2026, 9, 9, 13, 7, tzinfo=timezone.utc)
    number = "INC-0042_09.09.2026_13:07"
    assert incident_short_label(number) == "#42"
    assert incident_seq(number) == 42
    assert incident_when_label(number, now) == "13:07"
    older = "INC-0007_08.09.2026_09:13"
    assert incident_when_label(older, now) == "08.09 09:13"
    stamp = datetime(2026, 9, 9, 13, 7, 12, 987654, tzinfo=timezone.utc)
    shown = format_started_at(stamp)
    assert "987654" not in shown
    assert shown.endswith(_appliance_local(stamp).strftime("%H:%M:%S"))
    assert short_when_label(stamp, now) == _appliance_local(stamp).strftime("%H:%M")
    yesterday = datetime(2026, 9, 8, 9, 13, tzinfo=timezone.utc)
    assert short_when_label(yesterday, now) == _appliance_local(yesterday).strftime("%d.%m %H:%M")
    assert severity_pill("CRITICAL") == "crit"
    assert severity_pill("WARNING") == "warn"
    assert severity_pill("info") == "warn"


def test_dashboard_recent_incidents_uses_scan_columns():
    db = _db()
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    section = home.text.split("Recent incidents", 1)[1]
    headers = _headers(section)
    assert headers.index(">Hostname<") < headers.index(">Name<")
    assert ">Problem<" not in headers
    assert ">Severity<" in headers
    assert ">Status<" in headers
    assert ">When<" in headers
    assert headers.index(">When<") < headers.index(">Acknowledged<") < headers.index(">Resolved by<")
    assert ">Ack<" not in headers and "info-tip" not in headers
    assert ">Asset<" not in headers
    assert "Reported to" not in headers
    assert "inc-cell" in section
    assert "ack-dot" in section
    assert "scan-list" in section
    assert 'id="host-down-banner"' not in home.text
    assert "Recent journal" not in home.text
    kpi = home.text.split("dash-tiles-incidents", 1)[1].split("</section>", 1)[0]
    for label in (">Critical<", ">Warning<", ">Investigating<", ">Resolved<"):
        assert label in kpi
    assert ">Open<" not in kpi and ">Escalated<" not in kpi
    db.close()


def test_incidents_keeps_ack_resolved_by_and_drops_reported_to():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/incidents")
    assert page.status_code == 200
    headers = _headers(page.text)
    assert headers.index(">Hostname<") < headers.index(">Name<")
    assert ">When<" in headers
    assert ">Acknowledged<" in headers
    assert ">Ack<" not in headers and "info-tip" not in headers
    assert headers.index(">Acknowledged<") < headers.index(">Resolved by<")
    assert "Resolved by" in headers
    assert "Reported to" not in headers
    assert ">Asset<" not in headers
    assert "ack-dot" in page.text
    assert "inc-cell" in page.text
    assert 'class="stamp"' in page.text
    html = (ROOT / "frontend" / "templates" / "incidents.html").read_text(encoding="utf-8")
    assert "ack-dot" in html
    assert "Reported to" not in html
    assert "history.html" not in html
    nav = page.text.split('<aside class="nav">', 1)[1].split("</aside>", 1)[0]
    assert ">History<" not in nav
    db.close()


def test_mail_outbox_keeps_classic_when_to_subject_columns():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/ops")
    assert page.status_code == 200
    mail = page.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    headers = _headers(mail)
    assert ">When<" in headers
    assert ">To<" in headers
    assert ">Subject<" in headers
    assert ">Status<" in headers
    assert ">Body<" in headers
    assert ">Mail<" not in headers
    assert "scan-list" not in mail
    assert "mail-list" not in mail
    html = (ROOT / "frontend" / "templates" / "ops.html").read_text(encoding="utf-8")
    assert "scan-list mail-list" not in html
    assert "<th>When</th><th>To</th><th>Subject</th><th>Status</th><th>Body</th>" in html
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".mail-list" not in css
    assert ".scan-list tr.inc-ok td:first-child" in css
    db.close()
