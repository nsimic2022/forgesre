"""Pager Export offers TXT (unchanged) and CSV UTF-8 with the visible table columns."""

from __future__ import annotations

import csv
from io import StringIO
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.history import add_note
from app.inventory import create_manual_asset
from app.journal import report
from app.main import app
from app.models import AuditLog, DiscoveryCandidate, Incident, JournalEntry, Notification, Playbook, Playrule, ScheduledReport, utcnow
from app.seed import seed
from app.services import next_incident_number


def _token() -> str:
    """Letter-only id so leftover rows cannot trip page assertions like ``403 not in html``."""
    return "".join("abcdef"[int(ch, 16) % 6] for ch in uuid4().hex[:8])


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _csv_rows(body: bytes) -> list[list[str]]:
    assert body.startswith(b"\xef\xbb\xbf")
    text = body.decode("utf-8-sig")
    return list(csv.reader(StringIO(text)))


def test_pager_export_offers_txt_and_csv():
    _db().close()
    html = _client().get("/incidents").text
    form = html.split('action="/incidents/export"', 1)[1].split("</form>", 1)[0]
    assert 'name="format"' in form
    assert 'value="txt"' in form and 'value="csv"' in form
    assert ">TXT<" in form and ">CSV UTF-8<" in form
    home = _client().get("/").text
    dash = home.split('action="/incidents/export"', 1)[1].split("</form>", 1)[0]
    assert 'value="csv"' in dash


def test_incident_csv_keeps_serbian_notes_and_table_columns():
    db = _db()
    token = _token()
    asset = create_manual_asset(
        db,
        hostname=f"csv-host-{token}",
        ip=f"10.91.{uuid4().int % 200 + 1}.9",
        actor="tester",
    )
    number = next_incident_number(db)
    row = Incident(
        number=number,
        title=f"HighCPU {token}",
        severity="CRITICAL",
        status="OPEN",
        fingerprint=f"csv:{token}",
        started_at=utcnow(),
        asset_id=asset.id,
    )
    db.add(row)
    db.commit()
    add_note(db, row, "ana@dc.local", "Disk je pun, čekamo čišćenje na /var.")
    db.close()

    client = _client()
    plain = client.post("/incidents/export", data={"selected": number})
    assert plain.status_code == 200
    assert plain.headers["content-type"].startswith("text/plain")
    assert 'filename="incidents.txt"' in plain.headers["content-disposition"]
    assert number in plain.text
    assert "Disk je pun, čekamo čišćenje na /var." in plain.text

    exported = client.post("/incidents/export", data={"selected": number, "format": "csv"})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert 'filename="incidents.csv"' in exported.headers["content-disposition"]
    assert exported.content.startswith(b"\xef\xbb\xbf")
    table = _csv_rows(exported.content)
    assert table[0] == ["Hostname", "Incident", "Status", "Escalation", "When", "Severity", "Resolved by", "Notes"]
    assert len(table) == 2
    host, title, status, _ladder, _when, severity, resolved, notes = table[1]
    assert host == f"csv-host-{token}"
    assert title == f"HighCPU {token}"
    assert status == "OPEN"
    assert severity == "CRITICAL"
    assert resolved == "—"
    assert "čekamo čišćenje" in notes
    assert "č" in notes and "š" in notes


def test_other_list_csv_exports_use_table_headers():
    db = _db()
    token = _token()
    asset = create_manual_asset(db, hostname=f"csv-a-{token}", ip=f"10.92.{uuid4().int % 200 + 1}.4", actor="tester")
    found = DiscoveryCandidate(
        ip=f"10.93.{uuid4().int % 200 + 1}.5",
        hostname=f"found-{token}",
        proposed_role="Linux Server",
        status="new",
        source="scan",
        node_exporter=True,
    )
    rule = Playrule(name=f"rule-{token}", severity="warning", enabled=True, condition={"alertname": f"Rule{token}"})
    book = Playbook(name=f"book-{token}", slug=f"book-{token}", steps=[{"title": "check disk"}])
    db.add_all([found, rule, book])
    db.flush()
    journal = JournalEntry(module="jobs", action="csv", status="ok", summary=f"journal {token}")
    mail = Notification(target=f"csv-{token}@example.local", subject=f"mail {token}", body="telo", status="generated", step_key="manual")
    scheduled = ScheduledReport(name=f"report-{token}", to_email=f"csv-{token}@example.local", interval_hours=6)
    audit = AuditLog(actor="admin@forgesre.local", action=f"csv.{token}", object_type="layout", object_id="1")
    db.add_all([journal, mail, scheduled, audit])
    db.commit()
    asset_key = asset.asset_id
    found_id, rule_id, book_id = found.id, rule.id, book.id
    journal_id, mail_id, report_id, audit_id = journal.id, mail.id, scheduled.id, audit.id
    db.close()

    client = _client()
    assets = client.post("/assets/export", data={"selected": asset_key, "format": "csv"})
    assert assets.status_code == 200
    assert 'filename="assets.csv"' in assets.headers["content-disposition"]
    asset_table = _csv_rows(assets.content)
    assert asset_table[0][0] == "Hostname" and "Notes" in asset_table[0]
    assert any(f"csv-a-{token}" in row for row in asset_table[1:])

    found_csv = client.post("/discovery/export", data={"selected": str(found_id), "format": "csv"})
    assert found_csv.status_code == 200
    found_table = _csv_rows(found_csv.content)
    assert found_table[0][0] == "Hostname"
    assert any(f"found-{token}" in row for row in found_table[1:])

    rules = client.post("/playrules/export", data={"selected": str(rule_id), "format": "csv"})
    books = client.post("/playbooks/export", data={"selected": str(book_id), "format": "csv"})
    journal_csv = client.post("/journal/export", data={"selected": str(journal_id), "format": "csv"})
    mail_csv = client.post("/ops/mail/export", data={"selected": str(mail_id), "format": "csv"})
    reports = client.post("/ops/reports/export", data={"selected": str(report_id), "format": "csv"})
    audits = client.post("/admin/audit/export", data={"selected": str(audit_id), "format": "csv"})
    for response, name in (
        (rules, "playrules.csv"),
        (books, "playbooks.csv"),
        (journal_csv, "journal.csv"),
        (mail_csv, "mail.csv"),
        (reports, "scheduled-reports.csv"),
        (audits, "audit.csv"),
    ):
        assert response.status_code == 200, name
        assert f'filename="{name}"' in response.headers["content-disposition"], name
        assert response.content.startswith(b"\xef\xbb\xbf"), name
        assert len(_csv_rows(response.content)) >= 2, name
