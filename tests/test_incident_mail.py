"""Multipart HTML incident-report and escalation mail."""

from app.db import Base, SessionLocal, engine
from app.mailhtml import compose_email_message, incident_report_html, notification_html
from app.models import Asset, Incident, Investigation, Playbook
from app.seed import seed
from app.services import (
    build_incident_report,
    ensure_notification,
    next_incident_number,
    send_incident_report,
    send_outbound_mail,
)
from app.settings import settings


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _enable_smtp(monkeypatch):
    monkeypatch.setitem(settings.yaml["notifications"]["email"], "enabled", True)


def _capture_smtp(monkeypatch):
    sent = []

    class DummySMTP:
        def __init__(self, host, port, timeout=10):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            return None

        def login(self, user, password):
            return None

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("smtplib.SMTP", DummySMTP)
    return sent


def _demo_incident(db, *, severity="CRITICAL", title="Disk full"):
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    playbook = db.query(Playbook).filter_by(slug="disk-full").first()
    incident = Incident(
        number=next_incident_number(db),
        title=title,
        severity=severity,
        status="OPEN",
        fingerprint=f"html-mail-demo:{next_incident_number(db)}:forge-demo-01",
        asset_id=asset.id,
        playbook_id=playbook.id if playbook else None,
        summary="Demo host disk is full.",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    incident.asset = asset
    incident.playbook = playbook
    return incident


def _prod_incident(db, *, severity="WARNING", title="CPU high"):
    n = next_incident_number(db)
    asset = Asset(
        asset_id=f"app-html-mail-{n}",
        hostname="app-01",
        ip="10.10.10.50",
        type="Linux Server",
        owner="ops",
        contact_name="Ops on-call",
        owner_email="ops@dc.local",
        owner_phone="+381-11-111",
        notes="",
    )
    db.add(asset)
    db.flush()
    incident = Incident(
        number=n,
        title=title,
        severity=severity,
        status="OPEN",
        fingerprint=f"html-mail-prod:{n}:app-01",
        asset_id=asset.id,
        summary="Production CPU is high.",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    incident.asset = asset
    return incident


def _payload_types(message):
    raw = message.as_string()
    return raw, "text/plain" in raw, "text/html" in raw


def test_compose_multipart_includes_plain_and_html():
    message = compose_email_message(
        sender="forgesre@local",
        to="ops@dc.local",
        subject="report",
        body="plain body",
        html_body="<p>html body</p>",
    )
    raw, has_plain, has_html = _payload_types(message)
    assert has_plain
    assert has_html
    assert "multipart/alternative" in raw
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    assert plain is not None and "plain body" in plain.get_content()
    assert html is not None and "html body" in html.get_content()


def test_compose_without_html_stays_plain():
    message = compose_email_message(
        sender="forgesre@local",
        to="ops@dc.local",
        subject="lab ping",
        body="hello from ForgeSRE",
    )
    raw, has_plain, has_html = _payload_types(message)
    assert has_plain
    assert not has_html
    assert "multipart/alternative" not in raw


def test_demo_incident_html_has_lab_banner_and_severity_color():
    db = _db()
    incident = _demo_incident(db)
    html = incident_report_html(incident)
    db.close()
    assert "ForgeSRE incident report" in html
    assert "[DEMO] lab only" in html
    assert "DEMO incident on forge-demo-01" in html
    assert 'bgcolor="#fbbf24"' in html
    assert "#b91c1c" in html
    assert "Disk full" in html
    assert "Alert summary" in html
    assert "ForgeRCA has not been run yet." in html
    assert "This is a snapshot. ForgeSRE does not execute playbooks." in html
    assert "Owner/contact" in html


def test_non_demo_html_has_no_lab_banner():
    db = _db()
    incident = _prod_incident(db)
    html = incident_report_html(incident)
    db.close()
    assert "[DEMO] lab only" not in html
    assert 'bgcolor="#fbbf24"' not in html
    assert "DEMO incident" not in html
    assert "lab only" not in html.lower()
    assert "ForgeSRE incident report" in html
    assert "#b45309" in html
    assert "app-01" in html


def test_forgerca_section_renders_lists():
    db = _db()
    incident = _demo_incident(db)
    db.add(
        Investigation(
            incident_id=incident.id,
            summary="Disk filling on /var",
            likely_cause="WAL growth",
            confidence=72,
            recommended_action="Truncate old WAL",
            result={
                "facts": [{"text": "disk 95%"}],
                "anomalies": [{"summary": "write spike"}],
                "hypotheses": [{"summary": "candidate WAL"}],
                "limitations": ["metrics only"],
            },
        )
    )
    db.commit()
    db.refresh(incident)
    html = incident_report_html(incident)
    plain = build_incident_report(db, incident)
    db.close()
    assert "ForgeRCA" in html
    assert "Disk filling on /var" in html
    assert "WAL growth" in html
    assert "72%" in html
    assert "Truncate old WAL" in html
    assert "disk 95%" in html
    assert "write spike" in html
    assert "candidate WAL" in html
    assert "Likely cause:" in plain
    assert "disk 95%" in plain


def test_send_incident_report_smtp_payload_is_multipart(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch)
    sent = _capture_smtp(monkeypatch)
    incident = _demo_incident(db)
    row = send_incident_report(db, incident, "ops@dc.local", actor="admin@forgesre.local")
    db.close()
    assert row.status == "sent"
    assert "Disk full" in row.body
    assert "<html" not in (row.body or "").lower()
    assert sent, "SMTP send_message was not called"
    raw, has_plain, has_html = _payload_types(sent[0])
    assert has_plain
    assert has_html
    assert "multipart/alternative" in raw
    html = sent[0].get_body(preferencelist=("html",)).get_content()
    assert "[DEMO] lab only" in html
    assert "ForgeSRE incident report" in html
    plain = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "ForgeSRE incident report" in plain
    assert "Disk full" in plain


def test_send_incident_report_non_demo_html_has_no_banner(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch)
    sent = _capture_smtp(monkeypatch)
    incident = _prod_incident(db, severity="CRITICAL")
    send_incident_report(db, incident, "ops@dc.local", actor="admin@forgesre.local")
    db.close()
    html = sent[0].get_body(preferencelist=("html",)).get_content()
    assert "[DEMO] lab only" not in html
    assert "DEMO incident" not in html
    assert "#b91c1c" in html


def test_escalation_smtp_payload_is_multipart_with_demo_banner(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch)
    sent = _capture_smtp(monkeypatch)
    incident = _demo_incident(db, severity="WARNING", title="High CPU")
    note = ensure_notification(db, incident, "immediate")
    db.close()
    assert note.status == "sent"
    assert "High CPU" in note.body
    assert "Escalation step:" in note.body
    assert sent, "SMTP send_message was not called"
    raw, has_plain, has_html = _payload_types(sent[0])
    assert has_plain
    assert has_html
    html = sent[0].get_body(preferencelist=("html",)).get_content()
    assert "[DEMO] lab only" in html
    assert "ForgeSRE incident report" in html
    assert "Escalation step" in html
    assert "#b45309" in html


def test_ops_compose_stays_plain_text(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch)
    sent = _capture_smtp(monkeypatch)
    row = send_outbound_mail(
        db,
        target="ops@example.local",
        subject="lab ping",
        body="hello from ForgeSRE",
        step_key="manual",
    )
    db.close()
    assert row.status == "sent"
    assert row.body == "hello from ForgeSRE"
    raw, has_plain, has_html = _payload_types(sent[0])
    assert has_plain
    assert not has_html
    assert "multipart/alternative" not in raw


def test_notification_html_non_demo_has_no_lab_banner():
    db = _db()
    incident = _prod_incident(db)
    html = notification_html(incident, "immediate", "team")
    db.close()
    assert "[DEMO] lab only" not in html
    assert "lab only" not in html.lower()
    assert "Escalation step" in html
    assert "app-01" in html
