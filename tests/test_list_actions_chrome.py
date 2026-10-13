"""Pager-middle actions on the major lists.

Edit / Clone / Verify open one row or flash Select one. Export stays vertical text
with a break between records. Delete stays the existing bulk delete.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.backup import INNER_ARCHIVE, backup_compact, list_archives
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.migrate import migrate
from app.models import (
    AuditLog,
    DiscoveryCandidate,
    Incident,
    JournalEntry,
    Notification,
    Playbook,
    Playrule,
    ScheduledReport,
    User,
    utcnow,
)
from app.security import hash_password
from app.seed import seed
from app.services import ingest_alertmanager, next_incident_number

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    ">Import<",
    ">Check now<",
    ">Check Now<",
    ">Assign<",
    ">Bulk edit<",
    ">Bulk Edit<",
    ">Maintenance<",
    ">Dependencies<",
    ">CSV<",
    ">Excel<",
    ">View<",
    "Enable monitoring",
    "Disable monitoring",
)


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


def _middle(html: str) -> str:
    start = html.index("data-list-actions")
    return html[start : html.index('class="pager-size"', start)]


def _order(chunk: str, labels: list[str]) -> None:
    pos = -1
    for label in labels:
        found = chunk.index(f">{label}<", pos + 1)
        assert found > pos, label
        pos = found


def _no_invented(chunk: str) -> None:
    for label in FORBIDDEN:
        assert label not in chunk, label


def _separators(text: str, first: str, second: str) -> None:
    one = f"======== 1 of 2  {first} ========"
    two = f"======== 2 of 2  {second} ========"
    assert one in text
    assert two in text
    assert text.index(one) < text.index(two)
    assert "======== 1 of 1" not in text


def _box(html: str, value: str) -> str:
    match = re.search(rf'<input type="checkbox" name="selected" value="{value}"[^>]*>', html)
    assert match is not None, value
    return match.group(0)


def test_each_list_puts_its_actions_in_the_pager_middle():
    db = _db()
    client = _client()
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "[data-select-scope]:has([data-select-row]:checked) .list-actions" in css
    assert "form[data-delete-gated]" in js and "data-undeletable" in js
    assert "ids.length !== 1" in js and "Select one" in js
    assert "app.css?v=v09-T" in base and "app.js?v=v09-R" in base

    assets = _middle(client.get("/assets").text)
    _order(assets, ["Edit", "Clone", "Verify", "Export", "Remove"])
    assert 'action="/assets/open-edit"' in assets
    assert 'action="/assets/open-clone"' in assets
    assert 'action="/assets/open-verify"' in assets
    assert 'action="/assets/export"' in assets
    assert 'action="/assets/bulk-delete"' in assets
    _no_invented(assets)

    incidents = _middle(client.get("/incidents").text)
    _order(incidents, ["Edit", "Export", "Delete"])
    assert 'action="/incidents/edit"' in incidents
    assert 'action="/incidents/export"' in incidents
    assert 'action="/incidents/bulk-delete"' in incidents
    _no_invented(incidents)

    recent = client.get("/").text.split("<h2>Recent incidents</h2>", 1)[1].split("dash-graphs", 1)[0]
    recent_actions = _middle(recent)
    _order(recent_actions, ["Edit", "Export", "Delete"])
    assert 'action="/incidents/edit"' in recent_actions
    assert 'value="/"' in recent_actions or 'name="next" value="/"' in recent_actions
    _no_invented(recent_actions)

    discovery = client.get("/discovery").text
    found = _middle(discovery.split("<h2>Found assets</h2>", 1)[1])
    _order(found, ["Export", "Delete"])
    assert 'action="/discovery/export"' in found
    assert 'action="/discovery/bulk-delete"' in found
    assert ">Approve<" not in found
    assert ">Approve<" in discovery
    _no_invented(found)

    rules = client.get("/playrules").text
    rules_table = rules.split("<table", 1)[1].split("</table>", 1)[0]
    rules_actions = _middle(rules)
    _order(rules_actions, ["Edit", "Export", "Remove"])
    assert 'action="/playrules/open-edit"' in rules_actions
    assert 'action="/playrules/export"' in rules_actions
    assert 'action="/playrules/bulk-delete"' in rules_actions
    assert "/toggle" not in rules_actions
    assert 'class="onoff' in rules_table and "/toggle" in rules_table
    assert ">Edit<" not in rules_table and ">Remove<" not in rules_table
    _no_invented(rules_actions)

    books = client.get("/playbooks").text
    card = books.split("playbook-card", 1)[1].split("</section>", 1)[0]
    book_actions = _middle(books)
    _order(book_actions, ["Edit", "Export", "Remove"])
    assert 'action="/playbooks/open-edit"' in book_actions
    assert 'action="/playbooks/export"' in book_actions
    assert 'action="/playbooks/bulk-delete"' in book_actions
    assert ">Clone<" in card and ">Clone<" not in book_actions
    assert ">Edit<" not in card and ">Remove<" not in card
    _no_invented(book_actions)

    journal = client.get("/health-ui").text.split('id="journal"', 1)[1]
    journal_actions = _middle(journal)
    _order(journal_actions, ["Export", "Delete"])
    assert 'action="/journal/export"' in journal_actions
    assert 'action="/journal/bulk-delete"' in journal_actions
    assert "open-edit" not in journal_actions
    _no_invented(journal_actions)
    home = client.get("/").text
    assert 'action="/journal/export"' not in home
    assert 'action="/journal/bulk-delete"' not in home

    ops = client.get("/ops").text
    mail = ops.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    reports = ops.split('id="reports"', 1)[1]
    mail_actions = _middle(mail)
    report_actions = _middle(reports)
    _order(mail_actions, ["Export", "Delete"])
    assert 'action="/ops/mail/export"' in mail_actions
    assert 'action="/ops/mail/bulk-delete"' in mail_actions
    assert "data-delete-gated" in mail_actions
    _order(report_actions, ["Export", "Delete"])
    assert 'action="/ops/reports/export"' in report_actions
    assert 'action="/ops/reports/bulk-delete"' in report_actions
    assert "onoff" not in report_actions
    reports_tpl = (ROOT / "frontend" / "templates" / "ops.html").read_text(encoding="utf-8").split('id="reports"', 1)[1]
    assert 'class="onoff' in reports_tpl and "/ops/reports/{{ row.id }}/toggle" in reports_tpl
    assert ">Edit<" not in report_actions and ">Clone<" not in report_actions
    _no_invented(mail_actions)
    _no_invented(report_actions)

    admin = client.get("/admin").text
    audit = admin.split("<h2>Audit log</h2>", 1)[1].split("<h2>Platform backup", 1)[0]
    audit_actions = _middle(audit)
    assert ">Export<" in audit_actions
    assert 'action="/admin/audit/export"' in audit_actions
    assert ">Delete<" not in audit_actions
    assert "bulk-delete" not in audit_actions
    assert "open-edit" not in audit_actions
    _no_invented(audit_actions)
    db.close()


def test_edit_with_zero_or_two_rows_stays_on_the_list():
    db = _db()
    client = _client()
    rules = db.query(Playrule).order_by(Playrule.id).limit(2).all()
    books = db.query(Playbook).order_by(Playbook.id).limit(2).all()
    rule_ids = [str(row.id) for row in rules]
    book_ids = [str(row.id) for row in books]
    numbers = []
    for name in ("edit-a", "edit-b"):
        row = Incident(
            number=next_incident_number(db),
            title=name,
            severity="WARNING",
            status="OPEN",
            fingerprint=f"chrome-edit:{name}:{uuid4().hex[:6]}",
            started_at=utcnow(),
        )
        db.add(row)
        db.flush()
        numbers.append(row.number)
    db.commit()
    rule_name = rules[0].name
    book_name = books[0].name

    one_rule = client.post(
        "/playrules/open-edit",
        data={"selected": rule_ids[0], "stay": "/playrules?page=2"},
        follow_redirects=False,
    )
    assert one_rule.status_code == 303
    assert one_rule.headers["location"] == f"/playrules?page=2&edit={rule_ids[0]}#playrule-form"
    opened = client.get(one_rule.headers["location"].split("#", 1)[0])
    assert 'id="playrule-form-title">Edit rule' in opened.text

    many_rules = client.post(
        "/playrules/open-edit",
        data={"selected": rule_ids, "stay": "/playrules?page=2"},
        follow_redirects=False,
    )
    assert many_rules.status_code == 303
    assert many_rules.headers["location"] == "/playrules?page=2&edit_note=select-one"
    flashed = client.get(many_rules.headers["location"])
    assert 'role="status">Select one' in flashed.text
    assert 'id="playrule-form-title">Edit rule' not in flashed.text

    none_rules = client.post(
        "/playrules/open-edit",
        data={"stay": "https://evil.example/playrules"},
        follow_redirects=False,
    )
    assert none_rules.headers["location"] == "/playrules?edit_note=select-one"
    missing = client.post("/playrules/open-edit", data={"selected": "999999"}, follow_redirects=False)
    assert missing.headers["location"] == "/playrules?edit_note=select-one"
    db.expire_all()
    assert db.get(Playrule, int(rule_ids[0])).name == rule_name

    one_book = client.post(
        "/playbooks/open-edit",
        data={"selected": book_ids[0], "stay": "/playbooks?page=2"},
        follow_redirects=False,
    )
    assert one_book.status_code == 303
    assert one_book.headers["location"] == f"/playbooks?page=2&edit={book_ids[0]}#playbook-form"
    assert 'id="playbook-form"' in client.get("/playbooks?edit=" + book_ids[0]).text
    assert ">Edit playbook<" in client.get("/playbooks?edit=" + book_ids[0]).text

    many_books = client.post(
        "/playbooks/open-edit",
        data={"selected": book_ids, "stay": "/playbooks?page=2"},
        follow_redirects=False,
    )
    assert many_books.headers["location"] == "/playbooks?page=2&edit_note=select-one"
    book_flash = client.get(many_books.headers["location"])
    assert 'role="status">Select one' in book_flash.text
    assert ">Edit playbook<" not in book_flash.text
    none_books = client.post("/playbooks/open-edit", data={"stay": "https://evil.example/playbooks"}, follow_redirects=False)
    assert none_books.headers["location"] == "/playbooks?edit_note=select-one"
    db.expire_all()
    assert db.get(Playbook, int(book_ids[0])).name == book_name

    one_incident = client.post(
        "/incidents/edit",
        data={"selected": numbers[0], "next": "/incidents"},
        follow_redirects=False,
    )
    assert one_incident.status_code == 303
    assert one_incident.headers["location"].startswith(f"/incidents/{numbers[0]}")
    assert "focus=notes" in one_incident.headers["location"]
    many_incidents = client.post(
        "/incidents/edit",
        data={"selected": numbers, "next": "/incidents"},
        follow_redirects=False,
    )
    assert many_incidents.status_code == 303
    assert many_incidents.headers["location"] == "/incidents?edit_note=select-one"
    assert not re.search(r"/incidents/[^?]", many_incidents.headers["location"])
    none_incidents = client.post("/incidents/edit", data={"next": "/incidents"}, follow_redirects=False)
    assert none_incidents.headers["location"] == "/incidents?edit_note=select-one"
    dash = client.post("/incidents/edit", data={"selected": numbers, "next": "/"}, follow_redirects=False)
    assert dash.headers["location"] == "/?edit_note=select-one"
    assert 'role="status">Select one' in client.get(dash.headers["location"]).text

    from app.seed import DEMO_ASSET

    many_assets = client.post(
        "/assets/open-edit",
        data={"selected": [DEMO_ASSET, "forge-demo-02"], "stay": "/assets?q=forge"},
        follow_redirects=False,
    )
    assert "edit_note=select-one" in many_assets.headers["location"]
    assert "edit=" not in many_assets.headers["location"].split("edit_note", 1)[0]
    none_assets = client.post("/assets/open-edit", data={}, follow_redirects=False)
    assert none_assets.headers["location"].startswith("/assets?")
    assert "edit_note=select-one" in none_assets.headers["location"]
    assert "edit=" not in none_assets.headers["location"].split("edit_note", 1)[0]

    engineer = _client(_user(db, "engineer"))
    for path, selected in (
        ("/playrules/open-edit", rule_ids[0]),
        ("/playbooks/open-edit", book_ids[0]),
        ("/playbooks/bulk-delete", book_ids[0]),
        ("/playrules/bulk-delete", rule_ids[0]),
    ):
        assert engineer.post(path, data={"selected": selected}, follow_redirects=False).status_code == 403
    engineer_rules = _middle(engineer.get("/playrules").text)
    engineer_books = _middle(engineer.get("/playbooks").text)
    assert 'action="/playrules/export"' in engineer_rules
    assert "open-edit" not in engineer_rules and "bulk-delete" not in engineer_rules
    assert 'action="/playbooks/export"' in engineer_books
    assert "open-edit" not in engineer_books and "bulk-delete" not in engineer_books
    assert 'class="onoff' in engineer.get("/playrules").text
    db.close()


def test_exports_keep_the_record_separator():
    db = _db()
    token = uuid4().hex[:6]
    first_asset = create_manual_asset(db, hostname=f"exp-a-{token}", ip=f"10.81.{uuid4().int % 200 + 1}.4", actor="tester")
    second_asset = create_manual_asset(db, hostname=f"exp-b-{token}", ip=f"10.81.{uuid4().int % 200 + 1}.5", actor="tester")
    numbers = []
    for name in (f"exp-inc-a-{token}", f"exp-inc-b-{token}"):
        row = Incident(
            number=next_incident_number(db),
            title=name,
            severity="WARNING",
            status="OPEN",
            fingerprint=f"chrome:{name}",
            started_at=utcnow(),
        )
        db.add(row)
        db.flush()
        numbers.append(row.number)
    found = []
    for host, ip in ((f"found-a-{token}", f"10.82.{uuid4().int % 200 + 1}.6"), (f"found-b-{token}", f"10.82.{uuid4().int % 200 + 1}.7")):
        row = DiscoveryCandidate(ip=ip, hostname=host, proposed_role="Linux Server", status="new", source="scan")
        db.add(row)
        db.flush()
        found.append((host, str(row.id)))
    rules = []
    for name in (f"rule-a-{token}", f"rule-b-{token}"):
        row = Playrule(name=name, severity="warning", enabled=True, condition={"alertname": name})
        db.add(row)
        db.flush()
        rules.append((name, str(row.id)))
    books = []
    for name, slug in ((f"book-a-{token}", f"book-a-{token}"), (f"book-b-{token}", f"book-b-{token}")):
        row = Playbook(name=name, slug=slug, steps=[{"title": f"step {name}"}])
        db.add(row)
        db.flush()
        books.append((name, str(row.id)))
    journals = []
    for summary in (f"journal-a {token}", f"journal-b {token}"):
        row = JournalEntry(module="jobs", action="chrome", status="ok", summary=summary)
        db.add(row)
        db.flush()
        journals.append((summary, str(row.id)))
    mails = []
    for subject in (f"mail-a {token}", f"mail-b {token}"):
        row = Notification(target=f"{subject.split()[0]}@example.local", subject=subject, body="x", status="generated", step_key="manual")
        db.add(row)
        db.flush()
        mails.append((subject, str(row.id)))
    reports = []
    for name in (f"report-a-{token}", f"report-b-{token}"):
        row = ScheduledReport(name=name, to_email="ops@example.local", interval_hours=6)
        db.add(row)
        db.flush()
        reports.append((name, str(row.id)))
    audits = []
    for action in (f"chrome.a.{token}", f"chrome.b.{token}"):
        row = AuditLog(actor="admin@forgesre.local", action=action, object_type="layout", object_id="1")
        db.add(row)
        db.flush()
        audits.append((action, str(row.id)))
    db.commit()
    client = _client()

    assets = client.post("/assets/export", data={"selected": [first_asset.asset_id, second_asset.asset_id]}).text
    _separators(assets, first_asset.hostname, second_asset.hostname)
    alone = client.post("/assets/export", data={"selected": first_asset.asset_id}).text
    assert "======== 1 of 1" not in alone and first_asset.hostname in alone

    incidents = client.post("/incidents/export", data={"selected": numbers}).text
    _separators(incidents, numbers[0], numbers[1])
    discovered = client.post("/discovery/export", data={"selected": [found[0][1], found[1][1]]}).text
    _separators(discovered, found[0][0], found[1][0])
    play = client.post("/playrules/export", data={"selected": [rules[0][1], rules[1][1]]}).text
    _separators(play, rules[0][0], rules[1][0])
    exported_books = client.post("/playbooks/export", data={"selected": [books[0][1], books[1][1]]}).text
    _separators(exported_books, books[0][0], books[1][0])
    assert f"step {books[0][0]}" in exported_books
    assert 'filename="playbooks.txt"' in client.post("/playbooks/export", data={"selected": books[0][1]}).headers["content-disposition"]
    journal = client.post("/journal/export", data={"selected": [journals[0][1], journals[1][1]]}).text
    _separators(journal, journals[0][0], journals[1][0])
    mail = client.post("/ops/mail/export", data={"selected": [mails[0][1], mails[1][1]]}).text
    _separators(mail, mails[0][0], mails[1][0])
    scheduled = client.post("/ops/reports/export", data={"selected": [reports[0][1], reports[1][1]]}).text
    _separators(scheduled, reports[0][0], reports[1][0])
    audit = client.post("/admin/audit/export", data={"selected": [audits[0][1], audits[1][1]]}).text
    _separators(audit, audits[0][0], audits[1][0])
    db.close()


def test_playbook_remove_clears_links_and_many_rows_still_export():
    db = _db()
    token = uuid4().hex[:6]
    kept = Playbook(name=f"keep-{token}", slug=f"keep-{token}", steps=[{"title": "Stay"}])
    gone_a = Playbook(name=f"gone-a-{token}", slug=f"gone-a-{token}", steps=[{"title": "Drop A"}])
    gone_b = Playbook(name=f"gone-b-{token}", slug=f"gone-b-{token}", steps=[{"title": "Drop B"}])
    db.add_all([kept, gone_a, gone_b])
    db.flush()
    rule = Playrule(name=f"link-{token}", severity="warning", enabled=True, condition={"alertname": f"Link{token}"}, playbook_id=gone_a.id)
    incident = Incident(
        number=next_incident_number(db),
        title=f"linked {token}",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"chrome-link:{token}",
        started_at=utcnow(),
        playbook_id=gone_a.id,
    )
    db.add_all([rule, incident])
    db.commit()
    kept_id, gone_ids = kept.id, [str(gone_a.id), str(gone_b.id)]
    rule_id, incident_id = rule.id, incident.id
    client = _client()
    removed = client.post("/playbooks/bulk-delete", data={"selected": gone_ids, "next": "/playbooks"}, follow_redirects=False)
    assert removed.status_code == 303
    assert removed.headers["location"] == "/playbooks"
    db.expire_all()
    assert db.get(Playbook, kept_id) is not None
    assert db.get(Playbook, int(gone_ids[0])) is None
    assert db.get(Playbook, int(gone_ids[1])) is None
    assert db.get(Playrule, rule_id).playbook_id is None
    assert db.get(Incident, incident_id).playbook_id is None
    db.close()


def test_outbox_delete_is_offered_only_for_deletable_rows():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(db, hostname=f"mail-gate-{token}", ip=f"10.83.{uuid4().int % 200 + 1}.8", actor="tester")
    incident = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": f"Gate{token}", "asset": asset.asset_id, "severity": "warning"},
                    "annotations": {"summary": f"gate {token}"},
                }
            ],
        },
    )[0]
    step = db.query(Notification).filter_by(incident_id=incident.id).one()
    manual = Notification(target=f"ops-{token}@example.local", subject=f"note {token}", body="hello", status="generated", step_key="manual")
    db.add(manual)
    db.commit()
    step_id, manual_id = step.id, manual.id
    client = _client()
    mail = client.get("/ops").text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    assert "data-undeletable" in _box(mail, step_id)
    assert "data-undeletable" not in _box(mail, manual_id)
    assert "data-delete-gated" in _middle(mail)
    posted = client.post(
        "/ops/mail/bulk-delete",
        data={"selected": [str(step_id), str(manual_id)], "next": "/ops#mail"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    db.expire_all()
    assert db.get(Notification, step_id) is not None
    assert db.get(Notification, manual_id) is None
    viewer = _client(_user(db, "viewer"))
    viewer_mail = viewer.get("/ops").text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    assert 'action="/ops/mail/export"' in viewer_mail
    assert 'action="/ops/mail/bulk-delete"' not in viewer_mail
    assert viewer.post("/ops/mail/bulk-delete", data={"selected": str(step_id)}, follow_redirects=False).status_code == 403
    db.close()


def test_backup_export_lists_selected_names_and_times(tmp_path, monkeypatch):
    db = _db()
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    older = "backup_20261007T100000Z"
    newer = "backup_20261008T143000Z"
    for name in (older, newer):
        folder = tmp_path / "backups" / name
        folder.mkdir(parents=True)
        (folder / INNER_ARCHIVE).write_bytes(b"x" * 16)
    client = _client()
    page = client.get("/admin").text
    backups = page.split("<h2>Platform backup", 1)[1].split("admin-backup-cli", 1)[0]
    actions = _middle(backups)
    _order(actions, ["Export", "Delete selected"])
    assert 'action="/admin/backups/export"' in actions
    assert 'action="/admin/backups/bulk-delete"' in actions
    assert ">Download<" not in actions and ">Remove<" not in actions
    assert ">Download</a>" in backups and ">Remove<" in backups
    _no_invented(actions)
    exported = client.post("/admin/backups/export", data={"selected": [older, newer]})
    assert exported.status_code == 200
    assert 'filename="backups.txt"' in exported.headers["content-disposition"]
    _separators(exported.text, older, newer)
    for name in (older, newer):
        when = backup_compact(next(row for row in list_archives() if row["name"] == name))
        assert when["date"] in exported.text and when["time"] in exported.text
        assert name in exported.text
    assert (tmp_path / "backups" / older).is_dir()
    analyst = _client(_user(db, "analyst"))
    assert analyst.post("/admin/backups/export", data={"selected": older}, follow_redirects=False).status_code == 403
    db.close()
