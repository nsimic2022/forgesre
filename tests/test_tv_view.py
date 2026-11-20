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
        start = now - timedelta(hours=2, minutes=i)
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
