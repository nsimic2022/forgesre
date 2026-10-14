"""Weekly on-call roster: frozen time picks the person; L1 oncall mails that address."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import add_oncall_slot, current_oncall
from app.inventory import create_manual_asset
from app.main import app
from app.models import EscalationPolicy, Notification, Playrule, utcnow
from app.seed import seed
from app.services import escalation_recipient, ingest_alertmanager
from app.settings import settings


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_frozen_time_selects_the_weekday_slot():
    db = _db()
    zone = ZoneInfo(settings.timezone)
    monday = datetime(2026, 10, 12, 10, 0, tzinfo=zone)
    assert monday.weekday() == 0
    add_oncall_slot(db, weekday=0, start="08:00", end="16:00", name="Ana", email="ana@dc.local", phone="+381-11-100")
    add_oncall_slot(db, weekday=1, start="08:00", end="16:00", name="Boris", email="boris@dc.local", phone="+381-11-200")
    db.commit()
    person = current_oncall(db, monday)
    assert person is not None
    assert person["name"] == "Ana"
    assert person["email"] == "ana@dc.local"
    assert person["phone"] == "+381-11-100"
    assert current_oncall(db, datetime(2026, 10, 12, 18, 0, tzinfo=zone)) is None
    assert current_oncall(db, datetime(2026, 10, 13, 10, 0, tzinfo=zone))["email"] == "boris@dc.local"
    db.close()


def test_l1_oncall_mails_the_current_address_and_dashboard_shows_them():
    db = _db()
    zone = ZoneInfo(settings.timezone)
    now = utcnow().astimezone(zone)
    token = uuid4().hex[:8]
    add_oncall_slot(
        db,
        weekday=now.weekday(),
        start="00:00",
        end="23:59",
        name="Dana",
        email=f"dana-{token}@dc.local",
        phone="+381-11-300",
    )
    policy = EscalationPolicy(
        slug=f"oc-{token}",
        name=f"Oncall {token}",
        steps=[{"after_minutes": 0, "target": "oncall", "channel": "email"}],
    )
    rule = Playrule(
        name=f"oc-rule-{token}",
        enabled=True,
        severity="warning",
        condition={"alertname": f"OncallProbe{token}"},
        escalation_policy=policy,
    )
    db.add(policy)
    db.add(rule)
    raw = uuid4().int
    asset = create_manual_asset(
        db,
        hostname=f"oc-{token}",
        asset_id=f"oc-{token}",
        ip=f"10.82.{raw % 200 + 1}.{(raw >> 8) % 200 + 1}",
        type="Linux Server",
        owner_email="owner-should-not-get-this@dc.local",
        actor="tester",
    )
    db.commit()
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": f"OncallProbe{token}",
                        "severity": "warning",
                        "asset": asset.asset_id,
                    },
                    "annotations": {"summary": "oncall probe"},
                }
            ],
        },
    )
    assert len(created) == 1
    mails = db.query(Notification).filter(Notification.incident_id == created[0].id).all()
    assert len(mails) == 1
    assert mails[0].target == f"dana-{token}@dc.local"
    assert escalation_recipient(created[0], "oncall", db=db, at=now) == f"dana-{token}@dc.local"
    client = _client()
    dash = client.get("/")
    assert dash.status_code == 200
    assert 'data-oncall' in dash.text
    assert f"dana-{token}@dc.local" in dash.text
    assert 'href="/escalation#oncall"' in dash.text
    page = client.get("/escalation")
    assert page.status_code == 200
    assert 'id="oncall"' in page.text
    assert 'data-oncall-form' in page.text
    assert "Dana" in page.text
    db.close()
