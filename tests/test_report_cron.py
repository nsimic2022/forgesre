"""Cron schedules for scheduled reports: parser, next run, API from the new UI."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.cron import next_after, parse

UTC = timezone.utc


def test_parse_rejects_bad_expressions():
    for bad in ("", "0 7 * *", "61 * * * *", "0 25 * * *", "*/0 * * * *", "0 7 * * funday", "5-1 * * * *"):
        with pytest.raises(ValueError):
            parse(bad)
    minutes, hours, _dom, months, dows, _, _ = parse("*/15 8-18/2 * jan,jul mon-fri")
    assert minutes == {0, 15, 30, 45} and hours == {8, 10, 12, 14, 16, 18}
    assert months == {1, 7} and dows == {1, 2, 3, 4, 5}
    assert parse("0 0 * * 7")[4] == {0}


def test_next_after_in_utc_and_local_time():
    wed = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)  # a Wednesday
    assert next_after("0 7 * * 1", wed) == datetime(2026, 11, 23, 7, 0, tzinfo=UTC)
    assert next_after("*/15 * * * *", wed) == datetime(2026, 11, 18, 10, 15, tzinfo=UTC)
    assert next_after("0 8 1 * *", wed) == datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
    # 07:00 in Belgrade (UTC+1 in November) is 06:00 UTC.
    assert next_after("0 7 * * *", wed, "Europe/Belgrade") == datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    # Day-of-month OR day-of-week when both are restricted (classic cron).
    assert next_after("0 9 20 * 1", wed) == datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_after("0 0 31 2 *", wed)


def test_schedule_label_and_advance_use_cron():
    from app.models import ScheduledReport
    from app.services import advance_next_run, build_schedule, schedule_label, weekday_allowed

    built = build_schedule(schedule="cron", cron=" 0  7 * * 1 ")
    assert built["cron"] == "0 7 * * 1" and built["preset"] == "cron"
    with pytest.raises(ValueError):
        build_schedule(schedule="cron", cron="nope")
    row = ScheduledReport(name="x", to_email="a@b.c", schedule={"repeat": True, "preset": "cron", "cron": "0 7 * * 1", "every_minutes": 1440})
    assert schedule_label(row) == "cron 0 7 * * 1"
    sch = {"repeat": True, "cron": "0 7 * * 1"}
    assert weekday_allowed(datetime(2026, 11, 18, tzinfo=UTC), sch) is True
    from zoneinfo import ZoneInfo

    from app.settings import settings

    after = datetime(2026, 11, 23, 7, 0, tzinfo=UTC)
    nxt = advance_next_run(after, sch)
    local = nxt.astimezone(ZoneInfo(settings.timezone or "UTC"))
    assert nxt > after and local.weekday() == 0 and (local.hour, local.minute) == (7, 0)


def _signed_in(role: str):
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.main import create_app
    from app.models import User
    from app.security import hash_password
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def test_scheduled_report_api_roles_cron_and_toggle():
    tag = uuid4().hex[:6]
    viewer = _signed_in("viewer")
    body = {"name": f"Weekly {tag}", "recipients": [f"noc-{tag}@dc.local"], "schedule": "cron", "cron": "0 7 * * 1"}
    assert viewer.post("/api/v1/scheduled-reports", json=body).status_code == 403
    analyst = _signed_in("analyst")
    assert analyst.post("/api/v1/scheduled-reports", json={**body, "cron": "0 7 * *"}).status_code == 400
    assert analyst.post("/api/v1/scheduled-reports", json={**body, "recipients": []}).status_code == 400
    made = analyst.post("/api/v1/scheduled-reports", json=body)
    assert made.status_code == 200, made.text
    row = made.json()
    assert row["schedule"] == "cron 0 7 * * 1" and row["spec"]["cron"] == "0 7 * * 1" and row["next_run_at"]
    listed = next(r for r in analyst.get("/api/v1/reports-page").json()["scheduled"] if r["id"] == row["id"])
    assert listed["spec"]["preset"] == "cron"
    changed = analyst.put(f"/api/v1/scheduled-reports/{row['id']}", json={**body, "schedule": "24h", "weekdays": [0, 1, 2, 3, 4]})
    assert changed.json()["schedule"].startswith("24h")
    assert analyst.post(f"/api/v1/scheduled-reports/{row['id']}/toggle").json()["enabled"] is False
    assert analyst.delete(f"/api/v1/scheduled-reports/{row['id']}").status_code == 200
    assert analyst.delete(f"/api/v1/scheduled-reports/{row['id']}").status_code == 404
