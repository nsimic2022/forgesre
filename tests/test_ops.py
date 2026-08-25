from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import MailContact, Notification, ScheduledReport, User
from app.security import hash_password
from app.seed import seed
from app.services import process_scheduled_reports

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
    assert "Core (container)" in page.text
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
    assert "Send now" in page.text
    assert "ops-compose" in page.text
    assert "ops-add-email" in page.text
    assert "max-width: 38%" in page.text
    assert "min-width: 58%" in page.text
    db.close()


def test_ops_compose_column_is_forced_wider_than_add_email():
    ops = (ROOT / "frontend" / "templates" / "ops.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "split ops-forms" not in ops
    assert "ops-add-email" in ops
    assert "ops-compose" in ops
    assert "max-width: 38%" in ops
    assert "min-width: 58%" in ops
    assert "max-width: 38%" in css
    assert "min-width: 58%" in css
    assert ".split { display: grid; grid-template-columns: 1.2fr 0.8fr;" in css


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


def test_ops_send_now_creates_outbox_without_waiting_or_schedule():
    db = _db()
    client = TestClient(app)
    _login(client)
    before = db.query(ScheduledReport).count()
    posted = client.post(
        "/ops/reports/send-now",
        data={"new_email": "now@example.local", "asset_id": "forge-demo-01"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == "/ops#mail"
    assert db.query(ScheduledReport).count() == before
    mail = (
        db.query(Notification)
        .filter_by(target="now@example.local", step_key="report")
        .order_by(Notification.id.desc())
        .first()
    )
    assert mail is not None
    assert mail.status == "generated"
    assert mail.incident_id is None
    assert mail.subject == "[ForgeSRE] send-now"
    assert "forge-demo-01" in mail.body
    assert "Not an incident" in mail.body
    assert "SMTP disabled" in (mail.error or "") or "not sent" in (mail.error or "").lower()
    page = client.get("/ops")
    assert "Send now" in page.text
    assert "now@example.local" in page.text
    db.close()


def test_ops_send_now_rejects_viewer():
    db = _db()
    if db.query(User).filter_by(email="viewer@forgesre.local").first() is None:
        db.add(User(email="viewer@forgesre.local", name="V", password_hash=hash_password("testpass"), role="viewer"))
        db.commit()
    client = TestClient(app)
    _login(client, "viewer@forgesre.local")
    posted = client.post(
        "/ops/reports/send-now",
        data={"new_email": "nope-now@example.local", "asset_id": "forge-demo-01"},
        follow_redirects=False,
    )
    assert posted.status_code == 403
    assert db.query(Notification).filter_by(target="nope-now@example.local", step_key="report").first() is None
    db.close()
