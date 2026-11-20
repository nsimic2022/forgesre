"""Analyst visibility: host under title, severity-tinted title, age for live incidents, mail header facts."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.incident_report_mail import build_incident_report, incident_report_html
from app.models import Asset, Incident
from app.notifications import build_escalation_html
from app.seed import seed
from app.services import duration_label, incident_host, incident_when, next_incident_number, short_when_label

ROOT = Path(__file__).resolve().parents[1]
AGE_RE = r"(?:<1m|\d+m|\d+h(?: \d+m)?|\d+d(?: \d+h)?)"
WALL_RE = r"(?:\d{2}:\d{2}|\d{2}\.\d{2} \d{2}:\d{2})"


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


def _add(db, *, status, severity, started, title, resolved_at=None, labels=None, asset=None):
    row = Incident(
        number=next_incident_number(db, started),
        title=title,
        severity=severity,
        status=status,
        fingerprint=f"visibility:{uuid4().hex}",
        started_at=started,
        resolved_at=resolved_at,
        resolved_by="admin@forgesre.local" if resolved_at else "",
        alert_payload={"labels": labels or {}},
        asset_id=asset.id if asset else None,
        summary="visibility",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _row(html: str, number: str) -> str:
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    for chunk in body.split("<tr")[1:]:
        if f'href="/incidents/{number}"' in chunk:
            return chunk
    raise AssertionError(f"{number} not in list")


def test_duration_label_buckets():
    assert duration_label(30) == "<1m"
    assert duration_label(18 * 60) == "18m"
    assert duration_label(2 * 3600 + 14 * 60) == "2h 14m"
    assert duration_label(3 * 3600) == "3h"
    assert duration_label(4 * 86400 + 3 * 3600 + 59) == "4d 3h"


def _when_cell(row_html: str) -> str:
    return '<td class="inc-when"' + row_html.split('<td class="inc-when"', 1)[1].split("</td>", 1)[0] + "</td>"


def test_closed_without_end_stamp_uses_last_timeline_step():
    start = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
    row = Incident(
        number="INC-0009_05.10.2026_08:00",
        title="x",
        status="CLOSED",
        severity="WARNING",
        started_at=start,
        timeline=[
            {"id": "a", "at": (start + timedelta(minutes=5)).isoformat()},
            {"id": "b", "at": (start + timedelta(hours=1, minutes=12)).isoformat()},
        ],
    )
    info = incident_when(row, start + timedelta(hours=5))
    assert info["primary"] == "1h 12m"
    assert info["secondary"]
    assert info["ended"]


def test_wall_clock_comes_from_started_at_not_id():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    row = Incident(number="INC-0010_01.01.2026_00:00", title="x", status="OPEN", severity="WARNING",
                   started_at=now - timedelta(minutes=18))
    info = incident_when(row, now)
    assert info["primary"] == "18m"
    assert info["secondary"] == short_when_label(row.started_at, now)
    assert "01.01" not in info["secondary"]


def test_incident_when_helper_live_and_done():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    live = Incident(number="INC-0001_05.10.2026_09:46", title="x", status="ESCALATED", severity="WARNING",
                    started_at=now - timedelta(hours=2, minutes=14))
    info = incident_when(live, now)
    assert info["live"] and info["primary"] == "2h 14m"
    assert info["secondary"] == short_when_label(live.started_at, now)
    assert info["title"].startswith("Open for 2h 14m · started ")
    assert info["ended"] == ""
    naive = Incident(number="INC-0002", title="x", status="INVESTIGATING", severity="WARNING",
                     started_at=(now - timedelta(minutes=18)).replace(tzinfo=None))
    assert incident_when(naive, now)["primary"] == "18m"
    done = Incident(number="INC-0003_01.10.2026_09:00", title="x", status="RESOLVED", severity="CRITICAL",
                    started_at=now - timedelta(days=4), resolved_at=now - timedelta(days=4) + timedelta(minutes=40))
    info = incident_when(done, now)
    assert not info["live"]
    assert info["primary"] == "40m"
    assert re.fullmatch(WALL_RE, info["secondary"])
    assert info["secondary"] == short_when_label(done.started_at, now)
    assert info["title"].startswith("Lasted 40m · started ")
    assert info["ended"] and info["ended_full"] in info["title"]


def test_incident_host_prefers_asset_then_labels():
    asset = Asset(asset_id="db-asset-01", hostname="db-01")
    assert incident_host(Incident(asset=asset, alert_payload={"labels": {"instance": "x:9100"}})) == "db-01"
    assert incident_host(Incident(alert_payload={"labels": {"asset": "sw-core-02"}})) == "sw-core-02"
    assert incident_host(Incident(alert_payload={"labels": {"instance": "10.1.1.5:9182"}})) == "10.1.1.5"
    assert incident_host(Incident(alert_payload={"labels": {"asset": "unlabeled"}})) == ""
    assert incident_host(Incident(alert_payload={})) == ""


def test_report_and_escalation_html_lead_with_header_facts():
    db = _db()
    now = datetime.now(timezone.utc)
    asset = Asset(
        asset_id=f"vis-mail-{uuid4().hex[:6]}",
        hostname="app-07",
        ip="10.10.10.70",
        owner="ops",
        contact_name="Ops on-call",
        owner_email="ops@dc.local",
        owner_phone="+381-11-111",
        notes="",
    )
    db.add(asset)
    db.flush()
    row = _add(
        db,
        status="OPEN",
        severity="CRITICAL",
        started=now - timedelta(hours=2, minutes=14, seconds=5),
        title="Mail header check",
        asset=asset,
    )
    row.asset = asset
    report = incident_report_html(row, db)
    escalation = build_escalation_html(row, "immediate", "team")
    plain = build_incident_report(db, row)
    db.close()
    for html in (report, escalation):
        head = html.split('class="meta"', 1)[1].split("</table>", 1)[0]
        assert "Mail header check" in html.split('class="meta"', 1)[0]
        assert "app-07" in head
        assert "Duration" in head
        assert re.search(r"2h 1[45]m \(still open\)", head)
        assert "First" in head
        assert "Who to call" in head
        assert "Ops on-call · ops@dc.local · +381-11-111" in head
        assert "Details" in html
    assert re.search(r"Duration: 2h 1[45]m", plain)
    assert "Host: app-07" in plain
    assert "+00:00" not in report.split("Details", 1)[0]


