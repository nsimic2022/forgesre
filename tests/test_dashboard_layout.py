"""Dashboard layout pass: section order (no alarm banners), big tiles, appliance card, incidents heat, per-page select, row checkboxes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import or_

from app.db import Base, SessionLocal, engine
from app.history import (
    PAGE_SIZE,
    PAGE_SIZE_CHOICES,
    incident_heat,
    paginate,
    pager_state,
    parse_per_page,
    per_page_param,
)
from app.main import app
from app.models import AuditLog, DiscoveryCandidate, Incident, JournalEntry, Notification, ScheduledReport
from app.seed import seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _drop_layout_rows():
    """Shared sqlite DB: later tests assume a small incident count (next_incident_number), so remove what we add."""
    yield
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(Incident).filter(
        or_(Incident.fingerprint.like("layout:%"), Incident.fingerprint.like("NodeExporterDown:layout-%"))
    ).delete(synchronize_session=False)
    db.query(Notification).filter(Notification.step_key == "layout").delete(synchronize_session=False)
    db.query(ScheduledReport).filter(ScheduledReport.name.like("layout-%")).delete(synchronize_session=False)
    db.query(DiscoveryCandidate).filter(DiscoveryCandidate.ip.like("10.66.%")).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.action == "layout.test").delete(synchronize_session=False)
    db.query(JournalEntry).filter(JournalEntry.action == "layout").delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add_incidents(db, n: int, *, severity: str = "WARNING", status: str = "OPEN") -> str:
    token = uuid4().hex[:8]
    for i in range(n):
        db.add(
            Incident(
                number=next_incident_number(db),
                title=f"Layout {token} {i:03d}",
                severity=severity,
                status=status,
                fingerprint=f"layout:{token}:{i}",
                started_at=datetime.now(timezone.utc),
                summary="layout fixture",
            )
        )
        db.flush()
    db.commit()
    return token


def _tbody(html: str) -> str:
    return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def _rows(html: str) -> int:
    return _tbody(html).count("<tr")


def test_parse_per_page_default_choices_and_junk():
    assert PAGE_SIZE == 10
    assert PAGE_SIZE_CHOICES == (10, 20, 50, 100)
    assert parse_per_page(None) == 10
    assert parse_per_page("") == 10
    for n in PAGE_SIZE_CHOICES:
        assert parse_per_page(str(n)) == n
        assert parse_per_page(n) == n
    assert parse_per_page(" 50 ") == 50
    assert parse_per_page("abc") == 10
    assert parse_per_page("20; DROP TABLE") == 10
    assert parse_per_page("1e3") == 10
    assert parse_per_page("0") == 10
    assert parse_per_page("-20") == 10
    assert parse_per_page("15") == 20
    assert parse_per_page("1000") == 100
    assert parse_per_page("101") == 100


def test_per_page_param_pairs_with_page_key():
    assert per_page_param("page") == "per_page"
    assert per_page_param("reports_page") == "reports_per_page"
    assert per_page_param("audit_page") == "audit_per_page"
    assert per_page_param("backup_page") == "backup_per_page"


def test_paginate_honors_size_and_clamps():
    rows = list(range(250))
    ten, state = paginate(rows, "1")
    assert len(ten) == 10 and state["size"] == 10 and state["size_param"] == "per_page"
    twenty, state = paginate(rows, "2", size="20")
    assert twenty == list(range(20, 40))
    assert state["pages"] == 13
    capped, state = paginate(rows, "1", size="5000")
    assert len(capped) == 100 and state["size"] == 100
    junk, state = paginate(rows, "1", size="nope")
    assert len(junk) == 10
    assert pager_state("1", total=10)["show_size"] is False
    assert pager_state("1", total=11)["show_size"] is True


def test_incident_heat_levels():
    def tiles(**counts):
        return [{"key": key, "count": counts.get(key, 0)} for key in ("critical", "warning", "escalated")]

    assert incident_heat(tiles()) == ""
    assert incident_heat(tiles(escalated=0)) == ""
    assert incident_heat(tiles(warning=1)) == "warn"
    assert incident_heat(tiles(escalated=2)) == "warn"
    assert incident_heat(tiles(critical=1)) == "crit"
    assert incident_heat(tiles(warning=3, critical=1)) == "crit"


def _fake_doctor(**_kw):
    return {
        "overall": "DEGRADED",
        "failed": ["alertmanager"],
        "components": {
            "core": {"status": "ok"},
            "postgres": {"status": "ok"},
            "prometheus": {"status": "ok"},
            "alertmanager": {"status": "error", "why": "connection refused"},
            "grafana": {"status": "warn", "why": "graphs only"},
            "snmp": {"status": "paused"},
            "loki": {"status": "ok"},
            "alloy": {"status": "ok"},
            "llm": {"status": "disabled"},
            "netbox": {"status": "ok"},
            "discovery": {"status": "ok"},
            "mailpit": {"status": "ok"},
        },
    }


def _card(html: str) -> str:
    start = html.index("data-appliance-card")
    return html[start : html.index("</aside>", start)]


