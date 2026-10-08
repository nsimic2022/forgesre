"""Removing or switching off a scheduled report stops its mail. Escalation mail is a different path and is untouched."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app import services
from app.db import Base, SessionLocal, engine
from app.inventory import delete_asset
from app.main import app
from app.models import Asset, JournalEntry, Notification, ScheduledReport
from app.seed import seed
from app.services import process_scheduled_reports


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=1)


def _report(db, client, *, hours: str = "1", asset: str = "") -> ScheduledReport:
    token = uuid4().hex[:8]
    data = {"name": f"stop-{token}", "to_email": f"stop-{token}@example.local", "interval_hours": hours}
    if asset:
        data["asset_id"] = asset
    assert client.post("/ops/reports", data=data, follow_redirects=False).status_code == 303
    row = db.query(ScheduledReport).filter_by(name=data["name"]).one()
    row.next_run_at = _past()
    db.commit()
    return row


def _sent(db, target: str) -> int:
    db.expire_all()
    return db.query(Notification).filter_by(target=target, step_key="report").count()


def test_removed_report_is_never_sent_again():
    db = _db()
    client = _client()
    row = _report(db, client)
    target, pk = row.to_email, row.id
    assert client.post(f"/ops/reports/{pk}/delete", follow_redirects=False).status_code == 303
    for _ in range(3):
        process_scheduled_reports(SessionLocal())
    assert _sent(db, target) == 0
    assert db.get(ScheduledReport, pk) is None
    db.close()


def test_off_report_is_skipped_and_on_again_resumes():
    db = _db()
    client = _client()
    row = _report(db, client)
    target, pk = row.to_email, row.id
    client.post(f"/ops/reports/{pk}/toggle", follow_redirects=False)
    process_scheduled_reports(SessionLocal())
    assert _sent(db, target) == 0
    client.post(f"/ops/reports/{pk}/toggle", follow_redirects=False)
    process_scheduled_reports(SessionLocal())
    assert _sent(db, target) == 1
    process_scheduled_reports(SessionLocal())
    assert _sent(db, target) == 1, "Next moved one interval ahead; the next tick must not send again"
    db.close()


def test_switching_off_while_the_scheduler_pass_runs_is_honoured(monkeypatch):
    db = _db()
    client = _client()
    first = _report(db, client)
    second = _report(db, client)
    first_to, second_to, second_id = first.to_email, second.to_email, second.id
    real = services.send_performance_report

    def send_then_switch_off(*args, **kwargs):
        out = real(*args, **kwargs)
        if kwargs.get("to_email") == first_to:
            other = SessionLocal()
            other.get(ScheduledReport, second_id).enabled = False
            other.commit()
            other.close()
        return out

    monkeypatch.setattr(services, "send_performance_report", send_then_switch_off)
    process_scheduled_reports(SessionLocal())
    assert _sent(db, first_to) == 1
    assert _sent(db, second_to) == 0
    db.close()


def test_failure_after_smtp_hand_off_does_not_resend_every_tick(monkeypatch):
    db = _db()
    client = _client()
    row = _report(db, client)
    target = row.to_email
    handed_off: list[str] = []

    def smtp_ok_then_db_fails(db_, **kwargs):
        handed_off.append(kwargs["target"])
        raise RuntimeError("outbox commit failed after SMTP accepted the message")

    monkeypatch.setattr(services, "send_outbound_mail", smtp_ok_then_db_fails)
    for _ in range(4):
        process_scheduled_reports(SessionLocal())
    assert handed_off.count(target) == 1
    db.expire_all()
    errors = db.query(JournalEntry).filter_by(module="notification", action="report", status="error", object_id=str(row.id)).count()
    assert errors == 1
    db.close()


def test_one_broken_report_does_not_block_the_others():
    db = _db()
    client = _client()
    broken = _report(db, client)
    healthy = _report(db, client)
    broken.to_email = "not-an-address"
    db.commit()
    process_scheduled_reports(SessionLocal())
    assert _sent(db, healthy.to_email) == 1
    db.close()


def test_double_submit_does_not_leave_a_twin_that_keeps_mailing():
    db = _db()
    client = _client()
    token = uuid4().hex[:8]
    data = {"name": f"twin-{token}", "to_email": f"twin-{token}@example.local", "interval_hours": "6"}
    for _ in range(2):
        assert client.post("/ops/reports", data=data, follow_redirects=False).status_code == 303
    rows = db.query(ScheduledReport).filter_by(name=data["name"]).all()
    assert len(rows) == 1
    client.post(f"/ops/reports/{rows[0].id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.query(ScheduledReport).filter_by(name=data["name"]).count() == 0
    db.close()


def test_removing_the_only_asset_switches_the_report_off_instead_of_mailing_all():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    db.add(Asset(asset_id=f"rep-{token}", hostname=f"rep-{token}", type="Other", scrape_address=""))
    db.commit()
    row = _report(db, client, asset=f"rep-{token}")
    target, pk = row.to_email, row.id
    delete_asset(db, db.query(Asset).filter_by(asset_id=f"rep-{token}").one(), actor="test")
    db.expire_all()
    row = db.get(ScheduledReport, pk)
    assert row.asset_ids == []
    assert row.enabled is False
    process_scheduled_reports(SessionLocal())
    assert _sent(db, target) == 0
    db.close()
