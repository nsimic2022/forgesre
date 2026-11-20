"""Vertical exports, incident time range, mail subject, ICMP squares, backup stamp and bulk delete."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.incident_report_mail import build_incident_report, incident_report_html
from app.inventory import create_manual_asset
from app.main import app
from app.models import Incident
from app.notifications import build_escalation_html
from app.seed import seed
from app.services import (
    ensure_notification,
    incident_alarm_subject,
    incident_mail_heading,
    next_incident_number,
    send_incident_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(email: str = "admin@forgesre.local", password: str = "testpass") -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


def _field_line(text: str, label: str) -> str:
    for line in text.splitlines():
        if line.startswith(label + " ") or line.startswith(label + "  "):
            return line
    return ""


def test_email_subject_is_alarm_hostname_and_footer_is_gone():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(
        db,
        hostname=f"mail-{token}",
        ip=f"10.83.{uuid4().int % 200 + 1}.4",
        actor="tester",
        type="Other",
        owner_email="ops@dc.local",
    )
    incident = Incident(
        number=next_incident_number(db),
        title=f"CPU hot {token}",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"mailsub:{token}",
        summary="cpu",
        asset_id=asset.id,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    heading = incident_mail_heading(incident)
    assert heading.startswith(f"mail-{token} — ")
    assert f"CPU hot {token}" in heading
    assert incident.number not in heading
    subject = incident_alarm_subject(incident)
    assert subject == f"Alarm mail-{token}"
    assert f"CPU hot {token}" not in subject
    note = ensure_notification(db, incident, "immediate")
    assert note.subject == subject
    html = build_escalation_html(incident, "immediate", "team")
    assert heading in html
    assert f"mail-{token}" in html and f"CPU hot {token}" in html
    assert "does not execute playbooks" not in html
    assert "This is a snapshot" not in html
    plain = build_incident_report(db, incident)
    report_html = incident_report_html(incident, db)
    assert "does not execute playbooks" not in plain
    assert "This is a snapshot" not in plain
    assert heading in report_html
    assert f"CPU hot {token}" in report_html
    sent = send_incident_report(db, incident, "ops@dc.local", actor="admin@forgesre.local")
    assert sent.subject == subject

    from sqlalchemy.orm import joinedload

    from app.models import Asset

    demo_asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    demo_row = (
        db.query(Incident)
        .options(joinedload(Incident.asset))
        .filter(Incident.asset_id == demo_asset.id)
        .order_by(Incident.id.desc())
        .first()
    )
    assert demo_row is not None and demo_row.asset is not None
    demo_heading = incident_mail_heading(demo_row)
    demo_note = ensure_notification(db, demo_row, "15m")
    assert demo_note.subject == f"Alarm {demo_row.asset.hostname}"
    assert not demo_note.subject.startswith("[DEMO]")
    assert demo_row.title not in demo_note.subject
    demo_html = build_escalation_html(demo_row, "15m", "team")
    assert demo_heading in demo_html
    assert "[DEMO] lab only" in demo_html
    assert "DEMO incident" in (demo_note.body or "")
    db.close()


