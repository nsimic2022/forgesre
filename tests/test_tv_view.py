"""TV mode in the weather-report layout: day cards, the 24-hour curve and the tiles."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, MaintenanceWindow, utcnow
from app.seed import seed
from app.tv_view import tv_overview


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_days_hours_and_tiles_from_incidents():
    db = _db()
    now = utcnow()
    tag = uuid4().hex[:6]
    asset = Asset(asset_id=f"tvnoisy{tag}", hostname=f"tvnoisy{tag}", ip="10.88.0.9", type="Linux Server")
    db.add(asset)
    db.flush()
    for i in range(4):
        # mid-hour, so the four never straddle an hour boundary
        start = now.replace(minute=30, second=0, microsecond=0) - timedelta(hours=2, minutes=i)
        db.add(Incident(number=f"INC-TV{tag}{i}", title="Disk", severity="CRITICAL" if i == 0 else "HIGH", status="RESOLVED" if i else "OPEN",
                        fingerprint=f"tv:{tag}:{i}", asset_id=asset.id, started_at=start, ack_at=start + timedelta(minutes=5),
                        resolved_at=start + timedelta(minutes=30) if i else None))
    db.add(MaintenanceWindow(asset_ref=asset.asset_id, summary="patching", starts_at=now - timedelta(hours=5), ends_at=now - timedelta(hours=4)))
    db.commit()
    data = tv_overview(db, now)
    assert [d["label"] for d in data["days"]][-2:] == ["Yesterday", "Today"]
    assert data["days"][-1]["incidents"] >= 4 and data["days"][-1]["worst"] == "CRITICAL"
    assert len(data["hours"]["points"]) == 24
    hour = data["hours"]["points"][-3]
    assert hour["new"] >= 4 and hour["critical"] >= 1 and hour["acked_fast_pct"] is not None
    assert any(b["summary"] == "patching" for b in data["hours"]["maintenance"])
    tiles = {t["key"]: t for t in data["tiles"]}
    assert {"mtta", "mttr", "shift", "backup", "noisy", "forgeai", "hosts", "discovery"} <= set(tiles)
    # other tests share the database, so only check that our asset is counted right
    assert {"label": f"tvnoisy{tag}", "value": 4} in tiles["noisy"]["bars"] or tiles["noisy"]["value"] >= 4
    assert all(t["sentence"] for t in data["tiles"])
    assert all(t["gauge"] is None or 0 <= t["gauge"] <= 1 for t in data["tiles"])
    db.close()


def test_tv_api_carries_the_new_layout():
    db = _db()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})
    body = client.get("/api/v1/tv").json()
    assert {"days", "hours", "tiles", "counts", "clock"} <= set(body)


def test_day_clock_splits_one_day_into_am_and_pm():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from app.noc import incident_day

    db = _db()
    zone = ZoneInfo("Europe/Belgrade")
    day = (utcnow() - timedelta(days=3)).astimezone(zone).date()
    at = lambda h, m=0: datetime(day.year, day.month, day.day, h, m, tzinfo=zone).astimezone(timezone.utc)  # noqa: E731
    base = incident_day(db, day, "Europe/Belgrade")
    made = [
        Incident(number=f"INC-DAY-{uuid4().hex[:6]}", title="day-clock", severity=sev, status=status,
                 fingerprint=f"day:{uuid4().hex}", started_at=when)
        for sev, status, when in [("CRITICAL", "RESOLVED", at(14, 5)), ("CRITICAL", "RESOLVED", at(14, 40)),
                                  ("WARNING", "RESOLVED", at(14, 50)), ("HIGH", "RESOLVED", at(9)),
                                  ("CRITICAL", "SUPPRESSED", at(14)), ("WARNING", "RESOLVED", at(23, 59) + timedelta(minutes=2))]
    ]
    db.add_all(made)
    db.commit()
    try:
        out = incident_day(db, day, "Europe/Belgrade")
        assert [h["hour"] for h in out["am"]] == list(range(12)) and [h["hour"] for h in out["pm"]] == list(range(12, 24))
        # 14:00 local: two criticals and a warning; the suppressed one and the next-day one are left out
        delta = lambda half, i, k: out[half][i][k] - base[half][i][k]  # noqa: E731
        assert (delta("pm", 2, "critical"), delta("pm", 2, "warning")) == (2, 1)
        assert delta("am", 9, "high") == 1
        assert delta("pm", 11, "warning") == 0 and out["total"] - base["total"] == 4
        client = TestClient(create_app())
        client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})
        assert client.get(f"/api/v1/tv/day?date={day.isoformat()}").status_code == 200
        assert client.get("/api/v1/tv/day?date=yesterday").status_code == 400
        assert client.get(f"/api/v1/tv/day?date={(day + timedelta(days=10)).isoformat()}").status_code == 400
        assert client.get(f"/api/v1/tv/day?date={(day - timedelta(days=60)).isoformat()}").status_code == 400
    finally:
        for i in made:
            db.delete(i)
        db.commit()
        db.close()
