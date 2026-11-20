from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.models import Notification, ScheduledReport
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


