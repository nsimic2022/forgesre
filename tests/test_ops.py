from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import MailContact, Notification, ScheduledReport, User
from app.security import hash_password
from app.seed import seed
from app.services import process_scheduled_reports, send_outbound_mail
from app.settings import settings
from app.web import outbox_status_view

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_system_health_has_open_column_and_grafana():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/health-ui")
    assert page.status_code == 200
    assert "Open Grafana" in page.text
    assert "<th>Open</th>" in page.text
    assert "prometheus" in page.text
    assert "alloy" in page.text
    assert "grafana" in page.text
    assert "discovery" in page.text
    assert "GUI" in page.text
    assert "Metrics" in page.text
    db.close()


def test_ops_page_lists_outbox_and_reports():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/ops")
    assert page.status_code == 200
    assert "Email &amp; reports" in page.text or "Email & reports" in page.text
    send_at = page.text.find("Send email")
    outbox_at = page.text.find("Mail outbox")
    reports_at = page.text.find("Scheduled reports")
    assert 0 <= send_at < outbox_at < reports_at
    assert "Add email" in page.text
    assert "does not receive email" in page.text
    assert "./forgesre mailbox" in page.text
    assert "Gmail" in page.text
    assert "Outlook" in page.text
    assert "not enabled" in page.text
    assert "Open Grafana" not in page.text
    assert "Stack UIs" not in page.text
    assert "platform@forgesre.local" in page.text
    assert "not sent (email not configured)" in page.text
    assert "minmax(12rem, 0.4fr) minmax(20rem, 1.2fr)" in page.text
    assert "split ops-forms" in page.text
    db.close()


def test_ops_add_contact_then_pick_from_list():
    db = _db()
    client = TestClient(app)
    _login(client)
    posted = client.post(
        "/ops/contacts",
        data={"email": "storage@dc.local", "name": "Storage on-call"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    row = db.query(MailContact).filter_by(email="storage@dc.local").one()
    assert row.name == "Storage on-call"
    page = client.get("/ops")
    assert "storage@dc.local" in page.text
    assert "Storage on-call" in page.text
    db.close()


def test_ops_send_mail_lands_in_generated_outbox():
    db = _db()
    client = TestClient(app)
    _login(client)
    posted = client.post(
        "/ops/mail",
        data={"target": "ops@example.local", "subject": "lab ping", "body": "hello from ForgeSRE"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    row = db.query(Notification).filter_by(target="ops@example.local", step_key="manual").one()
    assert row.status == "generated"
    assert row.subject == "lab ping"
    assert "hello from ForgeSRE" in row.body
    assert row.incident_id is None
    assert "not sent (email not configured)" in (row.error or "")
    page = client.get("/ops")
    assert page.status_code == 200
    chunk = page.text.split("ops@example.local", 1)[1].split("</tr>", 1)[0]
    assert 'class="pill generated">generated</span>' in chunk
    assert 'class="muted outbox-hint">not sent (email not configured)</div>' in chunk
    assert 'class="pill sent"' not in chunk
    db.close()


def test_ops_report_run_now_creates_outbox_without_incident():
    db = _db()
    client = TestClient(app)
    _login(client)
    created = client.post(
        "/ops/reports",
        data={
            "name": "storage-6h",
            "to_email": "storage@example.local",
            "interval_hours": "6",
            "asset_id": "forge-demo-01",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    row = db.query(ScheduledReport).filter_by(name="storage-6h").one()
    assert row.to_email == "storage@example.local"
    assert row.interval_hours == 6
    assert row.asset_ids == ["forge-demo-01"]
    ran = client.post(f"/ops/reports/{row.id}/run", follow_redirects=False)
    assert ran.status_code == 303
    db.expire_all()
    mail = (
        db.query(Notification)
        .filter_by(target="storage@example.local", step_key="report")
        .order_by(Notification.id.desc())
        .first()
    )
    assert mail is not None
    assert mail.status == "generated"
    assert mail.incident_id is None
    assert "forge-demo-01" in mail.body
    assert "Not an incident" in mail.body
    db.close()


def test_ops_report_without_assets_is_all_inventory():
    db = _db()
    client = TestClient(app)
    _login(client)
    created = client.post(
        "/ops/reports",
        data={"name": "all-hosts", "to_email": "all@example.local", "interval_hours": "24"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    row = db.query(ScheduledReport).filter_by(name="all-hosts").one()
    assert row.asset_ids == []
    db.close()


def test_process_scheduled_reports_runs_when_due():
    db = _db()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    row = ScheduledReport(
        name="due-now",
        to_email="cron@example.local",
        interval_hours=6,
        asset_ids=["forge-demo-01"],
        enabled=True,
        next_run_at=past,
    )
    db.add(row)
    db.commit()
    ran = process_scheduled_reports(db)
    mail = db.query(Notification).filter_by(target="cron@example.local", step_key="report").first()
    assert mail is not None
    assert mail.status == "generated"
    db.refresh(row)
    assert row.last_run_at is not None
    assert row.next_run_at is not None
    assert ran >= 0
    db.close()


def test_viewer_can_read_ops_but_cannot_send():
    db = _db()
    if db.query(User).filter_by(email="viewer@forgesre.local").first() is None:
        db.add(User(email="viewer@forgesre.local", name="V", password_hash=hash_password("testpass"), role="viewer"))
        db.commit()
    client = TestClient(app)
    _login(client, "viewer@forgesre.local")
    assert client.get("/ops").status_code == 200
    posted = client.post(
        "/ops/mail",
        data={"target": "nope@example.local", "subject": "x", "body": "y"},
        follow_redirects=False,
    )
    assert posted.status_code == 403
    db.close()


def test_outbox_status_view_keeps_sent_failed_and_explains_generated():
    sent = outbox_status_view(SimpleNamespace(status="sent"))
    failed = outbox_status_view(SimpleNamespace(status="failed"))
    generated = outbox_status_view(SimpleNamespace(status="generated"))
    assert sent == {"css": "sent", "label": "sent", "hint": ""}
    assert failed == {"css": "failed", "label": "failed", "hint": ""}
    assert generated["css"] == "generated"
    assert generated["label"] == "generated"
    assert generated["hint"] == "not sent (email not configured)"
    assert generated["css"] != "sent"


def test_ops_compose_grid_is_on_that_template_only():
    ops = (ROOT / "frontend" / "templates" / "ops.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "minmax(12rem, 0.4fr) minmax(20rem, 1.2fr)" in ops
    assert ".split { display: grid; grid-template-columns: 1.2fr 0.8fr;" in css
    assert "minmax(12rem, 0.4fr)" not in css


def _enable_smtp(monkeypatch, host="smtp.gmail.com"):
    monkeypatch.setitem(settings.yaml["notifications"]["email"], "enabled", True)
    monkeypatch.setitem(settings.yaml["notifications"]["email"], "host", host)


def _capture_smtp(monkeypatch, fail=False):
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
            if fail:
                raise OSError("connection refused")
            sent.append(message)

    monkeypatch.setattr("smtplib.SMTP", DummySMTP)
    return sent


def test_ops_outbox_shows_sent_when_smtp_works(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch)
    captured = _capture_smtp(monkeypatch)
    row = send_outbound_mail(
        db,
        target="sent@example.local",
        subject="live ping",
        body="goes out",
        step_key="manual",
    )
    assert row.status == "sent"
    assert captured
    client = TestClient(app)
    _login(client)
    page = client.get("/ops")
    chunk = page.text.split("sent@example.local", 1)[1].split("</tr>", 1)[0]
    assert 'class="pill sent">sent</span>' in chunk
    assert "outbox-hint" not in chunk
    db.close()


def test_ops_outbox_shows_failed_when_smtp_raises(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch)
    _capture_smtp(monkeypatch, fail=True)
    row = send_outbound_mail(
        db,
        target="fail@example.local",
        subject="broken ping",
        body="did not leave",
        step_key="manual",
    )
    assert row.status == "failed"
    client = TestClient(app)
    _login(client)
    page = client.get("/ops")
    chunk = page.text.split("fail@example.local", 1)[1].split("</tr>", 1)[0]
    assert 'class="pill failed">failed</span>' in chunk
    assert "outbox-hint" not in chunk
    db.close()


def test_ops_outbox_not_sent_when_smtp_host_missing(monkeypatch):
    db = _db()
    _enable_smtp(monkeypatch, host="")
    row = send_outbound_mail(
        db,
        target="nosmtp@example.local",
        subject="lab only",
        body="stays here",
        step_key="manual",
    )
    assert row.status == "generated"
    assert "not sent (email not configured)" in (row.error or "")
    client = TestClient(app)
    _login(client)
    page = client.get("/ops")
    chunk = page.text.split("nosmtp@example.local", 1)[1].split("</tr>", 1)[0]
    assert 'class="pill generated">generated</span>' in chunk
    assert 'class="muted outbox-hint">not sent (email not configured)</div>' in chunk
    assert 'class="pill sent"' not in chunk
    db.close()
