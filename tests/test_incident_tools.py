"""Bulk acknowledge / resolve, "caused by" links and quiet-the-host from the incident page."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, User, utcnow
from app.security import hash_password
from app.seed import seed


def _client(role: str) -> TestClient:
    db = SessionLocal()
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def _incidents(n: int, asset: Asset | None = None) -> list[str]:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    tag = uuid4().hex[:6]
    out = []
    for i in range(n):
        row = Incident(number=f"INC-T{tag}{i}", title=f"tool {i}", severity="HIGH", status="OPEN", fingerprint=f"tool:{tag}:{i}",
                       started_at=utcnow() - timedelta(minutes=5), asset_id=asset.id if asset else None)
        db.add(row)
        out.append(row.number)
    db.commit()
    db.close()
    return out


def test_bulk_ack_and_resolve():
    numbers = _incidents(3)
    viewer, analyst = _client("viewer"), _client("analyst")
    assert viewer.post("/api/v1/incidents/bulk", json={"numbers": numbers, "action": "ack"}).status_code == 403
    assert analyst.post("/api/v1/incidents/bulk", json={"numbers": numbers, "action": "close"}).status_code == 400
    done = analyst.post("/api/v1/incidents/bulk", json={"numbers": numbers[:2] + ["INC-NOPE"], "action": "ack"}).json()
    assert done == {"done": numbers[:2], "missing": ["INC-NOPE"]}
    db = SessionLocal()
    assert all(db.query(Incident).filter_by(number=n).one().ack_at for n in numbers[:2])
    assert db.query(Incident).filter_by(number=numbers[2]).one().ack_at is None
    db.close()
    analyst.post("/api/v1/incidents/bulk", json={"numbers": numbers, "action": "resolve"})
    db = SessionLocal()
    assert {db.query(Incident).filter_by(number=n).one().status for n in numbers} == {"RESOLVED"}
    db.close()


def test_caused_by_link_and_no_loops():
    a, b, c = _incidents(3)
    analyst = _client("analyst")
    assert analyst.post(f"/api/v1/incidents/{b}/caused-by", json={"caused_by": a}).json()["caused_by"] == a
    assert analyst.post(f"/api/v1/incidents/{c}/caused-by", json={"caused_by": b}).status_code == 200
    assert analyst.post(f"/api/v1/incidents/{a}/caused-by", json={"caused_by": c}).status_code == 400  # loop
    assert analyst.post(f"/api/v1/incidents/{a}/caused-by", json={"caused_by": a}).status_code == 400
    view = analyst.get(f"/api/v1/incidents/{a}/view").json()["links"]
    assert view["caused_by"] is None and [r["number"] for r in view["consequences"]] == [b]
    assert analyst.get(f"/api/v1/incidents/{b}/view").json()["links"]["caused_by"]["number"] == a
    assert analyst.post(f"/api/v1/incidents/{b}/caused-by", json={"caused_by": ""}).json()["caused_by"] == ""


def test_quiet_host_puts_the_asset_in_maintenance():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").first()
    number = _incidents(1, asset)[0]
    db.close()
    analyst, engineer = _client("analyst"), _client("engineer")
    assert analyst.post(f"/api/v1/incidents/{number}/quiet-host", json={"hours": 2, "reason": "PSU swap"}).status_code == 403
    done = engineer.post(f"/api/v1/incidents/{number}/quiet-host", json={"hours": 2, "reason": "PSU swap"})
    assert done.status_code == 200 and done.json()["asset"] == "forge-demo-01"
    db = SessionLocal()
    from app.models import MaintenanceWindow

    assert db.query(MaintenanceWindow).filter_by(asset_ref="forge-demo-01").count() >= 1
    db.close()
    engineer.delete("/api/v1/maintenance/forge-demo-01")
