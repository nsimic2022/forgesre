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
from app.models import EscalationPolicy, Notification, OncallSlot, Playrule, utcnow
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
    db.query(OncallSlot).filter(OncallSlot.weekday == now.weekday()).delete(synchronize_session=False)
    db.commit()
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
    assert 'action="/escalation/oncall/open-edit"' in page.text
    assert 'action="/escalation/oncall/open-clone"' in page.text
    assert 'action="/escalation/oncall/bulk-delete"' in page.text
    assert f"/escalation/oncall/" in page.text and "/toggle" in page.text
    assert ">Remove<" in page.text and ">Edit<" in page.text
    assert "oncall-form-when" in page.text
    assert "oncall-form-who" in page.text
    assert page.text.index("oncall-form-when") < page.text.index("oncall-form-who")
    assert 'class="escalation-oncall-form"' in page.text
    assert "oncall-roster-wrap" in page.text
    assert 'data-oncall-person' in page.text
    db.close()


def test_disabled_slot_is_skipped_and_can_be_edited_cloned_removed():
    db = _db()
    zone = ZoneInfo(settings.timezone)
    now = utcnow().astimezone(zone)
    db.query(OncallSlot).filter(OncallSlot.weekday == now.weekday()).delete(synchronize_session=False)
    db.commit()
    slot = add_oncall_slot(
        db,
        weekday=now.weekday(),
        start="00:00",
        end="23:59",
        name="Eli",
        email="eli@dc.local",
        phone="+381-11-400",
    )
    db.commit()
    slot_id = slot.id
    client = _client()
    assert current_oncall(db, now)["email"] == "eli@dc.local"
    off = client.post(f"/escalation/oncall/{slot_id}/toggle", follow_redirects=False)
    assert off.status_code == 302
    db.expire_all()
    assert not db.get(OncallSlot, slot_id).enabled
    assert current_oncall(db, now) is None
    on = client.post(f"/escalation/oncall/{slot_id}/toggle", follow_redirects=False)
    assert on.status_code == 302
    db.expire_all()
    assert db.get(OncallSlot, slot_id).enabled

    opened = client.post(
        "/escalation/oncall/open-edit",
        data={"selected": str(slot_id), "stay": "/escalation"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert opened.headers["location"] == f"/escalation?oncall_edit={slot_id}#oncall-form"
    edit_page = client.get(f"/escalation?oncall_edit={slot_id}")
    assert 'id="oncall-form-title">Edit on-call' in edit_page.text
    assert 'value="Eli"' in edit_page.text
    saved = client.post(
        f"/escalation/oncall/{slot_id}/update",
        data={
            "weekday": str(now.weekday()),
            "start": "00:00",
            "end": "23:59",
            "name": "Eli Night",
            "email": "eli@dc.local",
            "phone": "+381-11-401",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 302
    db.expire_all()
    assert db.get(OncallSlot, slot_id).name == "Eli Night"

    cloned = client.post(
        "/escalation/oncall/open-clone",
        data={"selected": str(slot_id), "stay": "/escalation"},
        follow_redirects=False,
    )
    assert cloned.status_code == 303
    assert f"oncall_clone={slot_id}" in cloned.headers["location"]
    clone_page = client.get(f"/escalation?oncall_clone={slot_id}")
    assert 'id="oncall-form-title">Clone on-call' in clone_page.text
    assert 'value="Eli Night copy"' in clone_page.text
    copy = client.post(
        "/escalation/oncall",
        data={
            "weekday": str((now.weekday() + 1) % 7),
            "start": "08:00",
            "end": "16:00",
            "name": "Eli Night copy",
            "email": "eli@dc.local",
            "phone": "+381-11-401",
        },
        follow_redirects=False,
    )
    assert copy.status_code == 302
    copies = db.query(OncallSlot).filter(OncallSlot.name == "Eli Night copy").all()
    assert len(copies) == 1

    exported = client.post("/escalation/oncall/export", data={"selected": str(slot_id), "format": "csv"})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "When,Name,Email,Phone,Phone 2,Enabled" in exported.text.replace("\r", "")
    assert "Eli Night" in exported.text

    gone = client.post("/escalation/oncall/bulk-delete", data={"selected": str(slot_id), "next": "/escalation#oncall"}, follow_redirects=False)
    assert gone.status_code == 303
    db.expire_all()
    assert db.query(OncallSlot).filter_by(id=slot_id).first() is None
    db.close()


def test_oncall_edit_with_zero_or_two_rows_stays_on_the_list():
    db = _db()
    client = _client()
    first = add_oncall_slot(db, weekday=0, start="08:00", end="12:00", name="A", email="a@dc.local")
    second = add_oncall_slot(db, weekday=0, start="12:00", end="16:00", name="B", email="b@dc.local")
    db.commit()
    many = client.post(
        "/escalation/oncall/open-edit",
        data={"selected": [str(first.id), str(second.id)], "stay": "/escalation?oncall_page=1"},
        follow_redirects=False,
    )
    assert many.status_code == 303
    assert many.headers["location"] == "/escalation?oncall_page=1&edit_note=select-one#oncall"
    flashed = client.get("/escalation?oncall_page=1&edit_note=select-one")
    assert 'role="status">Select one' in flashed.text
    assert 'id="oncall-form-title">Edit on-call' not in flashed.text
    none = client.post("/escalation/oncall/open-edit", data={"stay": "https://evil.example/escalation"}, follow_redirects=False)
    assert none.headers["location"] == "/escalation?edit_note=select-one#oncall"
    db.close()
