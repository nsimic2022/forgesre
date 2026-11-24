"""Reports chart switcher: incidents per hour today, per day (7/30), per week (4 weeks)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Incident, utcnow
from app.reports_view import incident_trend
from app.seed import seed


def test_trend_buckets_and_api():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    now = utcnow()
    before = {r: incident_trend(db, r, now) for r in ("today", "7d", "4w", "30d")}
    made = [Incident(number=f"INC-TR-{uuid4().hex[:6]}", title="trend", severity=sev, status="RESOLVED",
                     fingerprint=f"tr:{uuid4().hex}", started_at=now - ago)
            for sev, ago in [("CRITICAL", timedelta(minutes=1)), ("HIGH", timedelta(days=2)), ("WARNING", timedelta(days=20))]]
    db.add_all(made)
    db.commit()
    try:
        after = {r: incident_trend(db, r, now) for r in ("today", "7d", "4w", "30d")}
        assert [len(after[r]["buckets"]) for r in ("today", "7d", "4w", "30d")] == [24, 7, 4, 30]
        assert after["today"]["unit"] == "hour" and after["4w"]["unit"] == "week"
        grew = {r: after[r]["total"] - before[r]["total"] for r in after}
        assert grew == {"today": 1, "7d": 2, "4w": 3, "30d": 3}
        assert after["today"]["buckets"][-1]["day"] == "23:00"
        assert sum(b["critical"] for b in after["today"]["buckets"]) - sum(b["critical"] for b in before["today"]["buckets"]) == 1
        client = TestClient(create_app())
        client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})
        assert client.get("/api/v1/reports/incident-trend?range=4w").json()["unit"] == "week"
        assert client.get("/api/v1/reports/incident-trend?range=year").status_code == 400
    finally:
        for i in made:
            db.delete(i)
        db.commit()
        db.close()


def test_today_period_and_ninety_day_weeks():
    from app.reports_view import incidents_csv, reports_overview

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    now = utcnow()
    before = reports_overview(db, 0, now)["current"]["incidents"]
    made = Incident(number=f"INC-TD-{uuid4().hex[:6]}", title="today", severity="CRITICAL", status="RESOLVED",
                    fingerprint=f"td:{uuid4().hex}", started_at=now - timedelta(seconds=5))
    db.add(made)
    db.commit()
    try:
        today = reports_overview(db, 0, now + timedelta(seconds=1))
        assert today["days"] == 0 and today["current"]["incidents"] == before + 1
        assert made.number in incidents_csv(db, 0, now + timedelta(seconds=1))
        ninety = incident_trend(db, "90d", now)
        assert ninety["unit"] == "week" and len(ninety["buckets"]) == 13
    finally:
        db.delete(made)
        db.commit()
        db.close()
