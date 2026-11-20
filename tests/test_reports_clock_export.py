"""Scheduled reports (once / weekdays / many recipients), richer exports, asset filters, clock, checkbox hit area."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.migrate import migrate
from app.models import ScheduledReport
from app.seed import seed
from app.services import (
    _appliance_local,
    compact_stamp,
    process_scheduled_reports,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=2)


def test_compact_stamp_drops_microseconds_epoch_and_iso_noise():
    stamp = datetime(2026, 10, 8, 12, 0, 1, 123456, tzinfo=timezone.utc)
    parts = compact_stamp(stamp)
    blob = parts["date"] + parts["time"]
    assert "123456" not in blob
    assert "T" not in blob and "Z" not in blob
    assert "Oct" in parts["date"] and "2026" in parts["date"]
    assert re.fullmatch(r"\d{2}:\d{2}", parts["time"])
    epoch = compact_stamp(1_760_000_000)
    assert epoch["date"] and "T" not in epoch["date"]
    assert compact_stamp("2026-10-08T21:57:32.123456+00:00")["time"].count(":") == 1
    assert "123456" not in compact_stamp("2026-10-08T21:57:32.123456+00:00")["time"]


def test_weekday_skip_does_not_send_and_moves_next():
    db = _db()
    token = uuid4().hex[:8]
    local_today = _appliance_local(datetime.now(timezone.utc)).weekday()
    other = (local_today + 2) % 7
    slot = _past()
    row = ScheduledReport(
        name=f"days-{token}",
        to_email=f"days-{token}@example.local",
        recipients=[f"days-{token}@example.local"],
        interval_hours=1,
        schedule={"repeat": True, "every_minutes": 15, "weekdays": [other], "preset": "15m"},
        enabled=True,
        next_run_at=slot,
    )
    db.add(row)
    db.commit()
    pk = row.id
    process_scheduled_reports(SessionLocal())
    from app.models import Notification

    db.expire_all()
    assert db.query(Notification).filter_by(target=f"days-{token}@example.local", step_key="report").count() == 0
    row = db.get(ScheduledReport, pk)
    assert row.last_run_at is None
    landed = row.next_run_at
    if landed.tzinfo is None:
        landed = landed.replace(tzinfo=timezone.utc)
    assert landed > datetime.now(timezone.utc) - timedelta(seconds=1)
    assert _appliance_local(landed).weekday() == other
    db.close()


