"""Weekly on-call roster: frozen time picks the person; L1 oncall mails that address."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import add_oncall_slot, current_oncall, oncall_directory, oncall_person_name, person_label
from app.main import app
from app.seed import seed
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


def test_oncall_person_name_is_not_a_role_label():
    assert oncall_person_name("Ana", "ana@dc.local") == "Ana"
    assert oncall_person_name("Super Admin", "admin@forgesre.local") == "Admin"
    assert oncall_person_name("Analyst", "ana@dc.local") == "Ana"
    assert oncall_person_name("super_admin", "admin@forgesre.local", "super_admin") == "Admin"
    assert person_label("Ana", "Kovač") == "Ana Kovač"
    assert person_label("Admin", "") == "Admin"


def test_oncall_directory_lists_users_as_people():
    db = _db()
    people = oncall_directory(db)
    by_email = {row["email"]: row for row in people}
    assert "admin@forgesre.local" in by_email
    assert by_email["admin@forgesre.local"]["name"] == "Admin"
    assert by_email["admin@forgesre.local"]["surname"] == ""
    assert by_email["admin@forgesre.local"]["label"] == "Admin"
    assert "Super Admin" not in by_email["admin@forgesre.local"]["name"]
    db.close()


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


