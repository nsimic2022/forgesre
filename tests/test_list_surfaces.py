"""Scan-friendly list language on Dashboard and Incidents."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.seed import seed
from app.services import (
    format_started_at,
    incident_seq,
    incident_short_label,
    incident_when_label,
    severity_pill,
    short_when_label,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient) -> None:
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )


def _headers(html: str) -> str:
    return html.split("<thead>", 1)[1].split("</thead>", 1)[0]


def test_list_helpers_short_id_when_and_severity():
    from app.services import _appliance_local

    now = datetime(2026, 9, 9, 13, 7, tzinfo=timezone.utc)
    number = "INC-0042_09.09.2026_13:07"
    assert incident_short_label(number) == "#42"
    assert incident_seq(number) == 42
    assert incident_when_label(number, now) == "13:07"
    older = "INC-0007_08.09.2026_09:13"
    assert incident_when_label(older, now) == "08.09 09:13"
    stamp = datetime(2026, 9, 9, 13, 7, 12, 987654, tzinfo=timezone.utc)
    shown = format_started_at(stamp)
    assert "987654" not in shown
    assert shown.endswith(_appliance_local(stamp).strftime("%H:%M:%S"))
    assert short_when_label(stamp, now) == _appliance_local(stamp).strftime("%H:%M")
    yesterday = datetime(2026, 9, 8, 9, 13, tzinfo=timezone.utc)
    assert short_when_label(yesterday, now) == _appliance_local(yesterday).strftime("%d.%m %H:%M")
    assert severity_pill("CRITICAL") == "crit"
    assert severity_pill("WARNING") == "warn"
    assert severity_pill("info") == "warn"


