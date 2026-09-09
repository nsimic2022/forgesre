"""Scan-friendly list language on Dashboard, History, Incidents, and Mail outbox."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Incident, Notification
from app.seed import seed
from app.services import (
    format_started_at,
    incident_seq,
    incident_short_label,
    incident_when_label,
    mail_purpose,
    mail_tone,
    next_incident_number,
    severity_pill,
    short_recipients,
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


def test_list_helpers_short_id_when_recipients_and_mail_heat():
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
    assert mail_tone("failed") == "mail-fail"
    assert mail_tone("sent") == "mail-sent"
    assert mail_tone("generated") == "mail-queued"
    assert short_recipients("ops@dc.local") == "ops"
    assert short_recipients("ops@dc.local, oncall@dc.local") == "ops +1"
    assert mail_purpose("incident-report") == "Incident report"
    assert mail_purpose("immediate") == "Escalation"


def test_dashboard_recent_incidents_uses_scan_columns():
    db = _db()
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    section = home.text.split("Recent incidents", 1)[1]
    headers = _headers(section)
    assert ">Incident<" in headers
    assert ">Severity<" in headers
    assert ">Status<" in headers
    assert ">When<" in headers
    assert ">Asset<" not in headers
    assert "Reported to" not in headers
    assert "inc-cell" in section
    assert "scan-list" in section
    assert 'id="host-down-banner"' in home.text
    kpi = home.text.split("Recent incidents", 1)[0]
    assert "Open</span>" in kpi or ">Open<" in kpi
    db.close()


def test_history_keeps_ack_and_drops_reported_to():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/history")
    assert page.status_code == 200
    headers = _headers(page.text)
    assert ">Incident<" in headers
    assert ">When<" in headers
    assert "Ack" in headers
    assert "Resolved" in headers
    assert "Reported to" not in headers
    assert ">Asset<" not in headers
    assert "ack-dot" in page.text
    assert "inc-cell" in page.text
    html = (ROOT / "frontend" / "templates" / "history.html").read_text(encoding="utf-8")
    assert "ack-dot" in html
    assert "Reported to" not in html
    db.close()


def test_mail_outbox_scan_columns_short_recipients_and_failed_heat():
    db = _db()
    now = datetime.now(timezone.utc)
    number = next_incident_number(db, now)
    incident = Incident(
        number=number,
        title="Outbox link",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"outbox-link:{uuid4().hex}",
        started_at=now,
        summary="outbox",
    )
    db.add(incident)
    db.flush()
    db.add(
        Notification(
            incident_id=incident.id,
            target="ops@dc.local, oncall@dc.local",
            subject="Disk snapshot for storage",
            body="full body with both addresses ops@dc.local",
            status="failed",
            step_key="incident-report",
            error="smtp 550",
            created_at=now,
        )
    )
    db.add(
        Notification(
            target="quiet@dc.local",
            subject="Nightly ok",
            body="sent body",
            status="sent",
            step_key="report",
            created_at=now - timedelta(days=1),
        )
    )
    db.commit()
    client = TestClient(app)
    _login(client)
    page = client.get("/ops")
    assert page.status_code == 200
    mail = page.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    headers = _headers(mail)
    assert ">Mail<" in headers
    assert ">Status<" in headers
    assert ">When<" in headers
    assert ">Body<" in headers
    assert ">Subject<" not in headers
    assert 'class="mail-list"' in mail
    assert "mail-row-fail" in mail
    assert "inc-row-done" in mail
    assert f">#{incident_seq(number)}<" in mail
    assert f'href="/incidents/{number}"' in mail
    assert "Disk snapshot for storage" in mail
    assert ">ops +1<" in mail or "ops +1" in mail
    assert "Incident report" in mail
    assert "To: ops@dc.local, oncall@dc.local" in mail
    assert "smtp 550" in mail
    db.close()
