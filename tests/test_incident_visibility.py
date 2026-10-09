"""Analyst visibility: host under title, severity-tinted title, age for live incidents, mail header facts."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.incident_report_mail import build_incident_report, incident_report_html
from app.main import app
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


def test_every_row_when_leads_with_duration_then_clock():
    db = _db()
    now = datetime.now(timezone.utc)
    live = _add(
        db,
        status="OPEN",
        severity="CRITICAL",
        started=now - timedelta(hours=2, minutes=14, seconds=10),
        title=f"Live disk {uuid4().hex[:6]}",
        labels={"instance": "10.20.30.40:9100"},
    )
    done = _add(
        db,
        status="CLOSED",
        severity="WARNING",
        started=now - timedelta(days=2, hours=1),
        resolved_at=now - timedelta(days=2),
        title=f"Closed cpu {uuid4().hex[:6]}",
    )
    resolved = _add(
        db,
        status="RESOLVED",
        severity="CRITICAL",
        started=now - timedelta(hours=3),
        resolved_at=now - timedelta(minutes=46),
        title=f"Resolved mem {uuid4().hex[:6]}",
    )
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    dash = home.text.split("Recent incidents", 1)[1]
    live_row = _row(dash, live.number)
    live_when = _when_cell(live_row)
    assert re.search(r'title="Open for 2h 1[45]m · started [^"]+"', live_when)
    assert 'data-when="stamp"' in live_when
    assert "inc-age" not in live_when
    assert re.search(r'<span class="stamp-date">\d{2} \w{3} \d{4}</span><span class="stamp-time">' + WALL_RE + "</span>", live_when)
    assert 'class="inc-title sev-crit"' in live_row
    assert 'class="inc-host"' in live_row and "10.20.30.40" in live_row
    assert "ack-dot" in live_row
    head = dash.split("</thead>", 1)[0]
    assert head.index(">Hostname<") < head.index(">Name<") < head.index(">Severity<") < head.index(">Status<")
    assert head.index(">When<") < head.index(">Acknowledged<") < head.index(">Resolved by<")
    assert ">Problem<" not in head and ">Ack<" not in head and "info-tip" not in head
    done_row = _row(dash, done.number)
    done_when = _when_cell(done_row)
    assert re.search(r'title="Lasted 1h · started [^"]+ · ended [^"]+"', done_when)
    assert 'data-when="stamp"' in done_when
    assert "inc-age" not in done_when
    assert "admin@forgesre.local" in done_row
    res_row = _row(dash, resolved.number)
    assert "inc-row-done" in res_row
    assert re.search(r'<span class="stamp-time">' + WALL_RE + "</span>", _when_cell(res_row))

    listed = client.get("/incidents")
    assert listed.status_code == 200
    inc_head = listed.text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert inc_head.index(">Hostname<") < inc_head.index(">Name<")
    assert ">Acknowledged<" in inc_head and "Resolved by" in inc_head
    assert ">Ack<" not in inc_head and "info-tip" not in inc_head
    live_row = _row(listed.text, live.number)
    live_when = _when_cell(live_row)
    assert re.search(r'title="Open for 2h 1[45]m · started [^"]+"', live_when)
    assert 'data-when="stamp"' in live_when
    assert "inc-age" not in live_when
    assert "INC-" not in live_when
    assert re.search(r'<span class="stamp-date">\d{2} \w{3} \d{4}</span><span class="stamp-time">' + WALL_RE + "</span>", live_when)
    assert 'class="inc-title sev-crit"' in live_row
    assert 'class="col-host"' in live_row and "10.20.30.40" in live_row
    assert '<span class="inc-host">· 10.20.30.40</span>' not in live_row
    assert "ack-dot" in live_row
    done_row = _row(listed.text, done.number)
    done_when = _when_cell(done_row)
    assert re.search(r'title="Lasted 1h · started [^"]+ · ended [^"]+"', done_when)
    assert 'data-when="stamp"' in done_when
    assert "inc-age" not in done_when
    assert "admin@forgesre.local" in done_row
    res_row = _row(listed.text, resolved.number)
    assert "inc-row-done" in res_row
    assert 'class="inc-title sev-crit"' in res_row
    assert re.search(r'<span class="stamp-time">' + WALL_RE + "</span>", _when_cell(res_row))
    db.close()


def test_incidents_default_all_shows_duration_for_resolved_without_filter():
    db = _db()
    now = datetime.now(timezone.utc)
    row = _add(
        db,
        status="RESOLVED",
        severity="WARNING",
        started=now - timedelta(minutes=58),
        resolved_at=now - timedelta(minutes=40),
        title=f"Default view {uuid4().hex[:6]}",
    )
    client = TestClient(app)
    _login(client)
    page = client.get("/incidents")
    assert page.status_code == 200
    when = _when_cell(_row(page.text, row.number))
    assert 'title="Lasted 18m' in when
    assert re.search(r'<span class="stamp-date">\d{2} \w{3} \d{4}</span><span class="stamp-time">' + WALL_RE + "</span>", when)
    assert "18m" not in when.split(">", 1)[1]
    db.close()


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


def test_detail_header_strip_has_first_and_duration():
    db = _db()
    now = datetime.now(timezone.utc)
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    row = _add(
        db,
        status="INVESTIGATING",
        severity="CRITICAL",
        started=now - timedelta(minutes=18, seconds=5),
        title="Strip check",
        asset=asset,
    )
    client = TestClient(app)
    _login(client)
    page = client.get(f"/incidents/{row.number}")
    assert page.status_code == 200
    strip = page.text.split("data-incident-strip", 1)[1].split("</header>", 1)[0]
    assert '<span class="sev-crit">Strip check</span>' in strip
    assert f'href="/assets/{asset.asset_id}"' in strip
    assert "First <span" in strip
    assert re.search(r"Open <span class=\"incident-fact\" data-incident-duration>1[89]m</span>", strip)
    assert 'class="pill investigating"' in strip
    assert 'class="pill crit"' in strip
    assert page.text.index("data-incident-strip") < page.text.index("Who to call")
    db.close()


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


def test_stack_cube_font_bumped_box_unchanged():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    block = css.split(".stack-cube {", 1)[1].split("}", 1)[0]
    size = float(re.search(r"font-size:\s*([\d.]+)rem", block).group(1))
    assert size > 0.7
    assert "padding: 0.28rem" in block
    assert "line-height: 0.77rem" in block
    assert "minmax(3.9rem, 1fr)" in css.split(".stack-cubes {", 1)[1].split("}", 1)[0]
    assert "app.css?v=v09-1" in (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
