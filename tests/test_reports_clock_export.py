"""Scheduled reports (once / weekdays / many recipients), richer exports, asset filters, clock, checkbox hit area."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import assets_matching, create_manual_asset
from app.main import app
from app.migrate import migrate
from app.models import Asset, AuditLog, EscalationPolicy, Incident, Playrule, ScheduledReport
from app.seed import seed
from app.services import (
    _appliance_local,
    compact_stamp,
    process_scheduled_reports,
    report_recipients,
    report_schedule,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=2)


def test_compact_stamp_drops_microseconds_epoch_and_iso_noise():
    stamp = datetime(2026, 10, 8, 12, 0, 1, 123456, tzinfo=timezone.utc)
    parts = compact_stamp(stamp)
    blob = parts["date"] + parts["time"]
    assert "123456" not in blob
    assert "T" not in blob and "Z" not in blob
    assert "Oct" in parts["date"] and "2026" in parts["date"]
    assert re.fullmatch(r"\d{2}:\d{2}", parts["time"])
    epoch = compact_stamp(1_760_000_000)
    assert epoch["date"] and "T" not in epoch["date"]
    assert compact_stamp("2026-10-08T21:57:32.123456+00:00")["time"].count(":") == 1
    assert "123456" not in compact_stamp("2026-10-08T21:57:32.123456+00:00")["time"]


def test_once_does_not_reschedule_and_delete_stops_the_send():
    db = _db()
    client = _client()
    token = uuid4().hex[:8]
    target = f"once-{token}@example.local"
    created = client.post(
        "/ops/reports",
        data={"name": f"once-{token}", "to_email": target, "schedule": "once"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    row = db.query(ScheduledReport).filter_by(name=f"once-{token}").one()
    assert report_schedule(row)["repeat"] is False
    row.next_run_at = _past()
    row.enabled = True
    db.commit()
    pk = row.id
    from app.models import Notification

    assert process_scheduled_reports(SessionLocal()) >= 1
    db.expire_all()
    row = db.get(ScheduledReport, pk)
    assert db.query(Notification).filter_by(target=target, step_key="report").count() == 1
    assert row.enabled is False
    assert row.next_run_at is None
    process_scheduled_reports(SessionLocal())
    db.expire_all()
    assert db.query(Notification).filter_by(target=target, step_key="report").count() == 1

    again = f"stop-{token}@example.local"
    assert client.post(
        "/ops/reports",
        data={"name": f"stop-{token}", "to_email": again, "schedule": "15m"},
        follow_redirects=False,
    ).status_code == 303
    job = db.query(ScheduledReport).filter_by(name=f"stop-{token}").one()
    job.next_run_at = _past()
    db.commit()
    assert client.post(f"/ops/reports/{job.id}/delete", follow_redirects=False).status_code == 303
    process_scheduled_reports(SessionLocal())
    db.expire_all()
    assert db.query(Notification).filter_by(target=again, step_key="report").count() == 0
    db.close()


def test_weekday_skip_does_not_send_and_moves_next():
    db = _db()
    token = uuid4().hex[:8]
    local_today = _appliance_local(datetime.now(timezone.utc)).weekday()
    other = (local_today + 2) % 7
    slot = _past()
    row = ScheduledReport(
        name=f"days-{token}",
        to_email=f"days-{token}@example.local",
        recipients=[f"days-{token}@example.local"],
        interval_hours=1,
        schedule={"repeat": True, "every_minutes": 15, "weekdays": [other], "preset": "15m"},
        enabled=True,
        next_run_at=slot,
    )
    db.add(row)
    db.commit()
    pk = row.id
    process_scheduled_reports(SessionLocal())
    from app.models import Notification

    db.expire_all()
    assert db.query(Notification).filter_by(target=f"days-{token}@example.local", step_key="report").count() == 0
    row = db.get(ScheduledReport, pk)
    assert row.last_run_at is None
    landed = row.next_run_at
    if landed.tzinfo is None:
        landed = landed.replace(tzinfo=timezone.utc)
    assert landed > datetime.now(timezone.utc) - timedelta(seconds=1)
    assert _appliance_local(landed).weekday() == other
    db.close()


def test_multiple_recipients_are_stored_and_each_gets_mail():
    db = _db()
    client = _client()
    token = uuid4().hex[:8]
    one, two, three = (f"{name}-{token}@example.local" for name in ("one", "two", "three"))
    posted = client.post(
        "/ops/reports",
        data={
            "name": f"multi-{token}",
            "to_email": [one, two],
            "extra_emails": three,
            "schedule": "30m",
            "weekday": ["0", "1", "2", "3", "4", "5", "6"],
        },
        follow_redirects=False,
    )
    assert posted.status_code == 303
    row = db.query(ScheduledReport).filter_by(name=f"multi-{token}").one()
    assert set(report_recipients(row)) == {one, two, three}
    assert report_schedule(row)["every_minutes"] == 30
    assert report_schedule(row)["repeat"] is True
    row.next_run_at = _past()
    db.commit()
    process_scheduled_reports(SessionLocal())
    from app.models import Notification

    db.expire_all()
    mailed = {
        target
        for (target,) in db.query(Notification.target).filter(Notification.step_key == "report", Notification.target.in_([one, two, three]))
    }
    assert mailed == {one, two, three}
    page = client.get("/ops").text
    reports = page.split('id="reports"', 1)[1]
    assert "Active scheduled reports" in reports
    assert "report-active-head" in reports
    assert "15 minutes" in reports and "Once (no repeat)" in reports and "Custom interval" in reports
    assert "Only these days" in reports
    assert 'name="extra_emails"' in reports
    assert one in reports and two in reports and three in reports
    db.close()


def test_history_playrule_and_who_exports_are_more_than_the_title():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    asset = create_manual_asset(
        db, hostname=f"hist-{token}", ip=f"10.88.{uuid4().int % 200 + 1}.8", actor="tester", type="Other"
    )
    from app.services import next_incident_number

    number = next_incident_number(db)
    row = Incident(
        number=number,
        title=f"Title only {token}",
        severity="CRITICAL",
        status="INVESTIGATING",
        fingerprint=f"export:{token}",
        started_at=datetime.now(timezone.utc) - timedelta(hours=3),
        summary=f"full body {token}",
        ack_by=f"ack-{token}@forgesre.local",
        resolved_by=f"res-{token}@forgesre.local",
        alert_payload={"labels": {"alertname": f"DiskFull{token}"}},
        asset_id=asset.id,
    )
    db.add(row)
    db.commit()
    exported = client.post("/incidents/export", data={"selected": number})
    assert exported.status_code == 200
    text = exported.text
    assert f"Title only {token}" in text
    assert f"hist-{token}" in text
    assert f"DiskFull{token}" in text
    assert "INVESTIGATING" in text
    assert f"full body {token}" in text
    assert f"ack-{token}@forgesre.local" in text
    assert f"res-{token}@forgesre.local" in text
    assert "123456" not in text

    policy = db.query(EscalationPolicy).order_by(EscalationPolicy.id).first()
    rule = Playrule(
        name=f"rule-{token}",
        severity="critical",
        enabled=True,
        condition={"alertname": f"CpuHot{token}", "metric": "cpu_usage", "operator": ">", "value": 90},
        escalation_policy_id=policy.id if policy else None,
    )
    db.add(rule)
    db.commit()
    rules = client.post("/playrules/export", data={"selected": str(rule.id)})
    assert rules.status_code == 200
    body = rules.text
    assert f"rule-{token}" in body
    assert f"CpuHot{token}" in body
    assert "cpu_usage" in body
    assert "not executed" in body
    assert "critical" in body
    if policy:
        assert policy.name in body

    audit = AuditLog(
        actor=f"who-{token}@forgesre.local",
        action="incident.ack",
        object_type="incident",
        object_id=number,
        data={"note": f"noc note {token}", "from": "desk"},
    )
    db.add(audit)
    db.commit()
    who = client.post(f"/incidents/{number}/audit/export", data={"selected": str(audit.id)})
    assert who.status_code == 200
    assert f"who-{token}@forgesre.local" in who.text
    assert "incident.ack" in who.text
    assert f"noc note {token}" in who.text
    assert "desk" in who.text
    assert "Oct" in who.text or "Jan" in who.text or re.search(r"[A-Z][a-z]{2} 20\d\d", who.text)
    detail = client.get(f"/incidents/{number}").text
    audit_html = detail.split('id="audit"', 1)[1].split("</section>", 1)[0]
    assert f"/incidents/{number}/audit/export" in audit_html
    assert 'class="stamp"' in audit_html
    db.close()


def test_assets_hostname_and_ip_filters_match_add_asset_names():
    db = _db()
    token = uuid4().hex[:6]
    host = create_manual_asset(
        db, hostname=f"filter-host-{token}", ip="10.50.50.21", actor="tester", asset_id=f"flt-{token}", type="Other"
    )
    other = create_manual_asset(
        db, hostname=f"other-host-{token}", ip="10.50.60.22", actor="tester", asset_id=f"oth-{token}", type="Other"
    )
    rows = db.query(Asset).filter(Asset.asset_id.in_([host.asset_id, other.asset_id])).all()
    assert {item.asset_id for item in assets_matching(rows, hostname=f"filter-host-{token}")} == {host.asset_id}
    assert {item.asset_id for item in assets_matching(rows, ip="10.50.60.22")} == {other.asset_id}
    assert assets_matching(rows, hostname=f"filter-host-{token}", ip="10.50.60.22") == []
    client = _client()
    page = client.get(f"/assets?hostname=filter-host-{token}")
    assert page.status_code == 200
    assert f'data-asset-reach="{host.asset_id}"' in page.text
    assert f'data-asset-reach="{other.asset_id}"' not in page.text
    form = page.text.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    assert form.count('name="q"') == 1
    assert 'name="hostname"' not in form and 'aria-label="Hostname"' not in form
    assert 'name="ip"' not in form and 'aria-label="IP"' not in form
    assert ">Type</option>" in form
    assert ">Source</option>" in form
    assert ">Site</option>" in form
    assert ">VLAN</option>" in form
    assert ">Customer</option>" in form
    assert "All types" not in form and "All sources" not in form and "All sites" not in form
    assert "All VLANs" not in form and "All customers" not in form and "Project" not in form
    ip_page = client.get("/assets?ip=10.50.60.22")
    assert f'data-asset-reach="{other.asset_id}"' in ip_page.text
    assert f'data-asset-reach="{host.asset_id}"' not in ip_page.text
    db.close()


def test_dashboard_clock_columns_checkbox_hit_and_admin_cards():
    db = _db()
    client = _client()
    home = client.get("/")
    assert home.status_code == 200
    nav = home.text.split('<aside class="nav">', 1)[1].split("</aside>", 1)[0]
    assert "nav-clock" in nav and "data-clock" in nav
    assert "appliance-clock" in home.text and "data-clock" in home.text
    recent = home.text.split("Recent incidents", 1)[1].split("</table>", 1)[0]
    assert recent.index(">Hostname<") < recent.index(">Name<") < recent.index(">Severity<") < recent.index(">Status<")
    assert recent.index(">When<") < recent.index(">Acknowledged<") < recent.index(">Resolved by<")
    assert ">Problem<" not in recent and ">Ack<" not in recent and "info-tip" not in recent.split("</thead>", 1)[0]
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    clock = css.split(".nav a.nav-clock {", 1)[1].split("}", 1)[0]
    assert "font-size: 2.4rem" in clock
    appliance = css.split(".appliance-clock {", 1)[1].split("}", 1)[0]
    assert "5.4rem" in appliance
    assert "label.row-check" in css and "min-height: 2.5rem" in css
    assert "width: 1rem" in css
    assert 'class="row-check"' in home.text
    play = client.get("/playrules").text
    actions = play.split("backup-row-actions", 1)[1].split("</div>", 1)[0]
    assert ">Edit<" in actions and ">Remove<" in actions
    admin = client.get("/admin").text
    assert "admin-user-split" in admin
    assert admin.count("admin-user-pane") >= 2
    assert "height: 100%" in css.split(".admin-user-split > .admin-user-pane", 1)[1].split("}", 1)[0]
    assert "margin: 2.25rem 0 0.7rem" in css
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v08-24" in base and "app.js?v=v08-24" in base
    db.close()
