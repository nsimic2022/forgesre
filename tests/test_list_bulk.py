"""List Delete / Export, and ON/OFF controls. Escalation mail is not deleted."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.bulk import mail_row_deletable
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.journal import report
from app.main import app
from app.migrate import migrate
from app.models import (
    Asset,
    AuditLog,
    DiscoveryCandidate,
    Incident,
    JournalEntry,
    Notification,
    Playrule,
    ScheduledReport,
    User,
    utcnow,
)
from app.security import hash_password
from app.seed import seed
from app.services import incident_seq, ingest_alertmanager, next_incident_number, process_escalations, process_scheduled_reports


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(email: str = "admin@forgesre.local", password: str = "testpass") -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


def _user(db, role: str) -> str:
    email = f"{role}-{uuid4().hex[:6]}@forgesre.local"
    db.add(User(email=email, name=role, password_hash=hash_password("testpass"), role=role))
    db.commit()
    return email


def test_deleted_incident_number_is_not_reused_and_viewer_cannot_delete():
    db = _db()
    number = next_incident_number(db)
    db.add(
        Incident(
            number=number,
            title="Bulk gone",
            severity="CRITICAL",
            status="OPEN",
            fingerprint=f"bulk:{uuid4().hex}",
            started_at=utcnow(),
        )
    )
    db.commit()
    seq = incident_seq(number)
    admin = _client()
    exported = admin.post("/incidents/export", data={"selected": number})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/plain")
    assert 'filename="incidents.txt"' in exported.headers["content-disposition"]
    assert number in exported.text
    assert "Bulk gone" in exported.text

    viewer = _user(db, "viewer")
    denied = _client(viewer)
    assert denied.post("/incidents/bulk-delete", data={"selected": number}, follow_redirects=False).status_code == 403
    assert db.query(Incident).filter_by(number=number).one()

    removed = admin.post("/incidents/bulk-delete", data={"selected": number, "next": "/incidents"}, follow_redirects=False)
    assert removed.status_code == 303
    assert removed.headers["location"] == "/incidents"
    db.expire_all()
    assert db.query(Incident).filter_by(number=number).first() is None
    assert incident_seq(next_incident_number(db)) == seq + 1
    page = admin.get("/incidents").text
    assert 'action="/incidents/bulk-delete"' in page
    assert 'action="/incidents/export"' in page
    assert "delete-selected" not in page
    db.close()


def test_deleting_an_incident_refreshes_the_asset_and_leaves_escalation_mail():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(db, hostname=f"bulk-host-{token}", ip=f"10.70.{uuid4().int % 200 + 1}.8", actor="tester")
    alertname = f"BulkFire{token}"
    incident = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": alertname, "asset": asset.asset_id, "severity": "critical"},
                    "annotations": {"summary": f"{alertname} on {asset.asset_id}"},
                }
            ],
        },
    )[0]
    number = incident.number
    asset_pk = asset.id
    db.expire_all()
    assert db.get(Asset, asset_pk).status == "critical"
    mail_ids = {row.id for row in db.query(Notification).filter_by(incident_id=incident.id)}
    assert mail_ids
    client = _client()
    assert client.post("/incidents/bulk-delete", data={"selected": number}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.query(Incident).filter_by(number=number).first() is None
    assert db.get(Asset, asset_pk).status == "healthy"
    # Outbox rows stay, detached, so the ladder has nothing left to send.
    kept = db.query(Notification).filter(Notification.id.in_(mail_ids)).all()
    assert {row.id for row in kept} == mail_ids
    assert all(row.incident_id is None for row in kept)
    db.close()


def test_outbox_delete_skips_escalation_steps_and_removes_operator_mail():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(db, hostname=f"mail-host-{token}", ip=f"10.71.{uuid4().int % 200 + 1}.9", actor="tester")
    incident = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": f"MailFire{token}", "asset": asset.asset_id, "severity": "warning"},
                    "annotations": {"summary": "mail fire"},
                }
            ],
        },
    )[0]
    step = db.query(Notification).filter_by(incident_id=incident.id).one()
    assert mail_row_deletable(step) is False
    manual = Notification(target=f"ops-{token}@example.local", subject="note", body="hello", status="generated", step_key="manual")
    db.add(manual)
    db.commit()
    step_id, manual_id = step.id, manual.id
    client = _client()
    before = {row.id for row in db.query(Notification).filter_by(incident_id=incident.id)}
    posted = client.post(
        "/ops/mail/bulk-delete",
        data={"selected": [str(step_id), str(manual_id)], "next": "/ops#mail"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    db.expire_all()
    assert db.get(Notification, manual_id) is None
    assert {row.id for row in db.query(Notification).filter_by(incident_id=incident.id)} == before
    process_escalations(db)
    db.expire_all()
    assert {row.id for row in db.query(Notification).filter_by(incident_id=incident.id)} == before
    viewer = _client(_user(db, "viewer"))
    assert viewer.post("/ops/mail/bulk-delete", data={"selected": str(step_id)}, follow_redirects=False).status_code == 403
    db.close()


def test_bulk_delete_of_a_scheduled_report_stops_the_scheduler():
    db = _db()
    client = _client()
    token = uuid4().hex[:8]
    assert client.post(
        "/ops/reports",
        data={"name": f"bulk-{token}", "to_email": f"bulk-{token}@example.local", "interval_hours": "1"},
        follow_redirects=False,
    ).status_code == 303
    row = db.query(ScheduledReport).filter_by(name=f"bulk-{token}").one()
    row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    report_id = row.id
    assert client.post("/ops/reports/bulk-delete", data={"selected": str(report_id)}, follow_redirects=False).status_code == 303
    process_scheduled_reports(SessionLocal())
    db.expire_all()
    assert db.get(ScheduledReport, report_id) is None
    assert db.query(Notification).filter_by(target=f"bulk-{token}@example.local", step_key="report").count() == 0
    page = client.get("/ops").text
    assert 'class="onoff' in Path("/workspace/frontend/templates/ops.html").read_text(encoding="utf-8")
    assert ">Toggle<" not in page
    assert ">Disable<" not in page.split('id="reports"', 1)[1]
    db.close()


def test_asset_discovery_playrule_and_journal_bulk_are_role_safe():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(db, hostname=f"rm-{token}", ip=f"10.72.{uuid4().int % 200 + 1}.4", actor="tester")
    asset_id = asset.asset_id
    found = DiscoveryCandidate(ip=f"10.73.{uuid4().int % 200 + 1}.{uuid4().int % 200 + 1}", proposed_role="Linux Server", status="new", source="scan")
    db.add(found)
    rule = Playrule(name=f"bulk-rule-{token}", severity="warning", enabled=True, condition={"alertname": f"Bulk{token}"})
    db.add(rule)
    db.commit()
    found_id, rule_id = found.id, rule.id
    report(db, "jobs", "bulk", "ok", summary=f"journal bulk {token}")
    entry = db.query(JournalEntry).filter(JournalEntry.summary == f"journal bulk {token}").one()
    entry_id = entry.id
    admin = _client()
    assets = admin.get("/assets").text
    assert 'action="/assets/bulk-delete"' in assets
    assert 'data-onoff' in assets
    assert 'name="alarm_cpu_enabled"' in assets
    assert ">ON<" in assets
    discovery = admin.get("/discovery").text
    assert ">Found assets<" in discovery
    assert 'action="/discovery/bulk-delete"' in discovery
    assert 'action="/discovery/export"' in discovery
    play = admin.get("/playrules").text
    assert ">Toggle<" not in play
    assert 'class="onoff' in play
    assert 'action="/playrules/bulk-delete"' in play

    exported = admin.post("/assets/export", data={"selected": asset_id})
    assert exported.status_code == 200
    assert asset_id in exported.text
    assert 'filename="assets.txt"' in exported.headers["content-disposition"]
    assert admin.post("/assets/bulk-delete", data={"selected": asset_id}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id=asset_id).first() is None

    found_txt = admin.post("/discovery/export", data={"selected": str(found_id)})
    assert found.ip in found_txt.text
    assert 'filename="found-assets.txt"' in found_txt.headers["content-disposition"]
    assert admin.post("/discovery/bulk-delete", data={"selected": str(found_id)}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(DiscoveryCandidate, found_id) is None

    engineer = _client(_user(db, "engineer"))
    assert engineer.post("/playrules/bulk-delete", data={"selected": str(rule_id)}, follow_redirects=False).status_code == 403
    assert db.get(Playrule, rule_id) is not None
    assert admin.post("/playrules/bulk-delete", data={"selected": str(rule_id)}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(Playrule, rule_id) is None

    journal = admin.get("/journal").text
    assert 'action="/journal/bulk-delete"' in journal
    assert 'name="selected"' in journal
    body = admin.post("/journal/export", data={"selected": str(entry_id)})
    assert f"journal bulk {token}" in body.text
    assert admin.post("/journal/bulk-delete", data={"selected": str(entry_id), "next": "/journal"}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(JournalEntry, entry_id) is None
    assert _client(_user(db, "viewer")).post("/journal/bulk-delete", data={"selected": "1"}, follow_redirects=False).status_code == 403
    home = admin.get("/").text
    assert "<h2>Recent incidents</h2>" in home
    assert "Recent journal" not in home
    assert 'action="/journal/export"' not in home
    assert 'action="/journal/bulk-delete"' not in home
    health = admin.get("/health-ui").text
    assert 'action="/journal/export"' in health.split('id="journal"', 1)[1]
    assert 'action="/journal/bulk-delete"' in health.split('id="journal"', 1)[1]
    db.close()


def test_audit_exports_and_has_no_delete_for_anyone():
    db = _db()
    row = AuditLog(actor="admin@forgesre.local", action=f"bulk.audit.{uuid4().hex[:6]}", object_type="layout", object_id="1")
    db.add(row)
    db.commit()
    admin = _client()
    page = admin.get("/admin").text
    audit = page.split("<h2>Audit log</h2>", 1)[1].split("<h2>Platform backup", 1)[0]
    assert 'action="/admin/audit/export"' in audit
    assert ">Delete<" not in audit
    assert "bulk-delete" not in audit
    exported = admin.post("/admin/audit/export", data={"selected": str(row.id)})
    assert exported.status_code == 200
    assert row.action in exported.text
    assert 'filename="audit.txt"' in exported.headers["content-disposition"]
    assert admin.post("/admin/audit/bulk-delete", data={"selected": str(row.id)}, follow_redirects=False).status_code == 404
    analyst = _client(_user(db, "analyst"))
    assert analyst.post("/admin/audit/export", data={"selected": str(row.id)}, follow_redirects=False).status_code == 403
    assert "backup-row-actions" in Path("/workspace/frontend/templates/admin.html").read_text(encoding="utf-8")
    assert ".backup-row-actions" in Path("/workspace/frontend/static/app.css").read_text(encoding="utf-8")
    db.close()
