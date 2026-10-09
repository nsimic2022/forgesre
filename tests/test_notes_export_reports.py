"""Notes column and Edit, report asset filters, TXT separators, per-asset HTML reports."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.history import incident_notes_snippet
from app.inventory import create_manual_asset
from app.journal import report
from app.main import app
from app.models import Incident, IncidentNote, JournalEntry, Notification, ScheduledReport, User, utcnow
from app.security import hash_password
from app.seed import seed
from app.services import (
    build_performance_report_html,
    incident_alarm_subject,
    next_incident_number,
    performance_report_message,
    report_mail_subject,
    send_outbound_mail,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(email: str = "admin@forgesre.local", password: str = "testpass") -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


def _user(db, email: str, role: str) -> None:
    row = db.query(User).filter_by(email=email).first()
    if row is None:
        db.add(User(email=email, name=role, password_hash=hash_password("testpass"), role=role))
    else:
        row.role = role
        row.password_hash = hash_password("testpass")
    db.commit()


def _ip() -> str:
    number = uuid4().int
    return f"10.{number % 200 + 1}.{(number >> 8) % 200 + 1}.{(number >> 16) % 200 + 1}"


def _incident(db, title: str, asset_id: int | None = None) -> Incident:
    row = Incident(
        number=next_incident_number(db),
        title=title,
        severity="WARNING",
        status="OPEN",
        fingerprint=f"notes:{uuid4().hex}",
        started_at=utcnow(),
        summary=title,
        asset_id=asset_id,
    )
    db.add(row)
    db.flush()
    return row


def _headers(html: str) -> str:
    return html.split("<thead>", 1)[1].split("</thead>", 1)[0]


def _notes_cell(row_html: str) -> str:
    return row_html.split('class="col-notes"', 1)[1].split("</td>", 1)[0]


def test_notes_snippet_uses_latest_operator_notes():
    db = _db()
    row = _incident(db, "snippet")
    db.add(IncidentNote(incident_id=row.id, actor="a", body="older write-up"))
    db.flush()
    db.add(IncidentNote(incident_id=row.id, actor="b", body="latest write-up"))
    db.commit()
    db.refresh(row)
    text = incident_notes_snippet(row)
    assert text.startswith("latest write-up")
    assert "older write-up" in text
    empty = _incident(db, "empty")
    db.commit()
    db.refresh(empty)
    assert incident_notes_snippet(empty) == ""
    db.close()


def test_incidents_and_dashboard_show_notes_and_edit_one_row():
    db = _db()
    token = uuid4().hex[:8]
    host = create_manual_asset(
        db,
        hostname=f"note-host-{token}",
        ip=_ip(),
        actor="tester",
        type="Other",
        asset_id=f"note-{token}",
    )
    older = f"older-{token}"
    latest = f"latest-{token}"
    filled = _incident(db, f"noted {token}", host.id)
    db.add(IncidentNote(incident_id=filled.id, actor="analyst", body=older))
    db.flush()
    db.add(IncidentNote(incident_id=filled.id, actor="engineer", body=latest))
    blank = _incident(db, f"blank {token}", host.id)
    db.commit()
    filled_number, blank_number = filled.number, blank.number
    before = db.query(IncidentNote).count()

    client = _client()
    page = client.get(f"/incidents?q={token}")
    assert page.status_code == 200
    head = _headers(page.text)
    assert head.index(">Resolved by<") < head.index(">Notes<")
    assert "<th" not in head.split(">Notes<", 1)[1]
    noted = page.text.split(f"noted {token}", 1)[1].split("</tr>", 1)[0]
    noted_cell = _notes_cell(noted)
    assert latest in noted_cell and older in noted_cell
    empty = page.text.split(f"blank {token}", 1)[1].split("</tr>", 1)[0]
    assert "—" in _notes_cell(empty)
    assert latest not in _notes_cell(empty)
    assert 'action="/incidents/edit"' in page.text
    assert "data-bulk-edit" in page.text

    home = client.get("/")
    recent = home.text.split("Recent incidents", 1)[1].split("</table>", 1)[0]
    assert recent.index(">Resolved by<") < recent.index(">Notes<")
    assert "<th" not in recent.split(">Notes<", 1)[1]
    dash_noted = home.text.split(f"noted {token}", 1)[1].split("</tr>", 1)[0]
    assert latest in _notes_cell(dash_noted)
    dash_blank = home.text.split(f"blank {token}", 1)[1].split("</tr>", 1)[0]
    assert "—" in _notes_cell(dash_blank)
    assert 'action="/incidents/edit"' in home.text.split("Recent incidents", 1)[1]

    one = client.post(
        "/incidents/edit",
        data={"selected": filled_number, "next": "/incidents"},
        follow_redirects=False,
    )
    assert one.status_code == 303
    assert one.headers["location"].startswith(f"/incidents/{filled_number}")
    assert "focus=notes" in one.headers["location"]
    assert one.headers["location"].endswith("#notes")
    detail = client.get(f"/incidents/{filled_number}?focus=notes")
    assert 'id="notes"' in detail.text
    assert "data-note-body" in detail.text
    assert "autofocus" in detail.text.split("data-note-body", 1)[1][:40]
    assert latest in detail.text
    assert db.query(IncidentNote).count() == before

    many = client.post(
        "/incidents/edit",
        data={"selected": [filled_number, blank_number], "next": "/incidents"},
        follow_redirects=False,
    )
    assert many.status_code == 303
    assert "edit_note=select-one" in many.headers["location"]
    assert "#notes" not in many.headers["location"]
    assert db.query(IncidentNote).count() == before
    flashed = client.get(many.headers["location"])
    assert 'role="status">Select one' in flashed.text

    none = client.post("/incidents/edit", data={"next": "/"}, follow_redirects=False)
    assert none.status_code == 303
    assert none.headers["location"].startswith("/?edit_note=select-one")
    assert db.query(IncidentNote).count() == before
    dash = client.get(none.headers["location"])
    assert "Select one" in dash.text.split("Recent incidents", 1)[0]

    _user(db, "viewer-notes@forgesre.local", "viewer")
    _user(db, "engineer-notes@forgesre.local", "engineer")
    viewer = _client("viewer-notes@forgesre.local")
    denied = viewer.get("/incidents")
    assert 'action="/incidents/edit"' not in denied.text
    blocked = viewer.post(
        "/incidents/edit",
        data={"selected": filled_number, "next": "/incidents"},
        follow_redirects=False,
    )
    assert blocked.status_code == 403
    engineer = _client("engineer-notes@forgesre.local")
    assert 'action="/incidents/edit"' in engineer.get("/incidents").text
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-bulk-edit" in js and "Select one" in js
    assert 'get("focus") === "notes"' in js
    db.close()


def test_report_asset_filter_persists_many_assets_and_recipients(monkeypatch):
    db = _db()
    token = uuid4().hex[:8]
    site = f"site-{token}"
    first = create_manual_asset(
        db,
        hostname=f"edge-{token}",
        ip=_ip(),
        actor="tester",
        type="Switch",
        asset_id=f"edge-{token}",
        extras={"site": site, "vlan": "20", "customer": f"Acme{token}"},
        notes=f"rack {token}",
    )
    second = create_manual_asset(
        db,
        hostname=f"core-{token}",
        ip=_ip(),
        actor="tester",
        type="Linux Server",
        asset_id=f"core-{token}",
        extras={"site": "other", "vlan": "30", "customer": "Other"},
    )
    client = _client()
    page = client.get("/ops")
    assert page.status_code == 200
    reports = page.text.split('id="reports"', 1)[1]
    assert reports.count("data-asset-picker") == 2
    schedule, send = reports.split("ops-report-now", 1)
    for block in (schedule, send):
        assert "data-asset-q" in block
        assert ">Type</option>" in block
        assert ">Source</option>" in block
        assert ">Site</option>" in block
        assert ">VLAN</option>" in block
        assert ">Customer</option>" in block
        assert "All types" not in block and "All sources" not in block
        assert "All sites" not in block and "All VLANs" not in block and "All customers" not in block
        assert ">Filter<" in block
        assert 'name="asset_id"' in block
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "label.check[hidden]" in css
    send_form = page.text.split("ops-report-now", 1)[1].split("</form>", 1)[0]
    assert send_form.count('name="to_email"') >= 1
    assert 'name="extra_emails"' in send_form
    row_html = page.text.split(f'value="{first.asset_id}"', 1)[0]
    # The label that owns this checkbox carries the same filter fields as /assets.
    label = page.text.split(f'value="{first.asset_id}"', 1)[0].rsplit("<label", 1)[1]
    assert f'data-type="Switch"' in label
    assert f'data-site="{site}"' in label
    assert 'data-vlan="20"' in label
    assert f'data-customer="Acme{token}"' in label
    assert "forge" in label
    assert first.ip in label
    assert f"rack {token}" in label
    del row_html

    name = f"multi-{token}"
    addresses = [f"a-{token}@example.local", f"b-{token}@example.local"]
    saved = client.post(
        "/ops/reports",
        data={"name": name, "to_email": addresses, "asset_id": [first.asset_id, second.asset_id], "schedule": "6h"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    row = db.query(ScheduledReport).filter_by(name=name).one()
    assert row.asset_ids == [first.asset_id, second.asset_id]
    assert [item.lower() for item in row.recipients] == addresses
    edit = client.get(f"/ops?edit={row.id}")
    form = edit.text.split('id="report-schedule-form"', 1)[1].split("</form>", 1)[0]
    for asset_id in (first.asset_id, second.asset_id):
        chunk = form.split(f'value="{asset_id}"', 1)[1][:80]
        assert "checked" in chunk
    for email in addresses:
        assert f'value="{email}"' in form and "checked" in form.split(f'value="{email}"', 1)[1][:80]

    monkeypatch.setattr("app.services.query_prometheus", lambda asset=None: {"cpu_percent": 12, "up": 1})
    seen: list[dict] = []

    def capture(db, **kwargs):
        seen.append(kwargs)
        return send_outbound_mail(db, **kwargs)

    monkeypatch.setattr("app.services.send_outbound_mail", capture)
    posted = client.post(
        "/ops/reports/send-now",
        data={"to_email": addresses, "asset_id": [second.asset_id, first.asset_id]},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    mails = (
        db.query(Notification)
        .filter(Notification.step_key == "report", Notification.target.in_(addresses))
        .order_by(Notification.id.asc())
        .all()
    )
    assert len(mails) == 2
    assert mails[0].body == mails[1].body
    assert mails[0].subject == mails[1].subject == "Report send-now"
    assert not mails[0].subject.startswith("Alarm")
    assert f"======== 1 of 2  {second.hostname} ========" in mails[0].body
    assert f"======== 2 of 2  {first.hostname} ========" in mails[0].body
    assert "does not execute playbooks" not in mails[0].body
    assert len(seen) == 2
    assert seen[0]["html"] == seen[1]["html"]
    assert seen[0]["body"] == seen[1]["body"] == mails[0].body
    assert seen[0]["subject"] == "Report send-now"
    html = seen[0]["html"]
    assert f"1 of 2 · {second.hostname}" in html
    assert f"2 of 2 · {first.hostname}" in html
    left, right = html.split(f"1 of 2 · {second.hostname}", 1)[1].split(f"2 of 2 · {first.hostname}", 1)
    assert second.asset_id in left and first.asset_id not in left
    assert first.asset_id in right
    assert "does not execute playbooks" not in html
    assert not str(seen[0]["subject"]).startswith("Alarm")
    direct = build_performance_report_html(db, [first.asset_id, second.asset_id], name="nightly")
    assert f"1 of 2 · {first.hostname}" in direct
    assert f"2 of 2 · {second.hostname}" in direct
    assert report_mail_subject("nightly") == "Report nightly"
    assert incident_alarm_subject(filled_alarm(db)).startswith("Alarm ")
    subject, body, html_again = performance_report_message(db, [first.asset_id, second.asset_id], "nightly")
    assert subject == "Report nightly"
    assert html_again == direct
    assert first.asset_id in body and second.asset_id in body
    db.close()


def filled_alarm(db):
    row = db.query(Incident).filter(Incident.asset_id.isnot(None)).first()
    return row


def test_multi_record_exports_have_titled_breaks():
    db = _db()
    token = uuid4().hex[:8]
    first = create_manual_asset(
        db, hostname=f"exp-a-{token}", ip=_ip(), actor="tester", type="Other", asset_id=f"expa-{token}"
    )
    second = create_manual_asset(
        db, hostname=f"exp-b-{token}", ip=_ip(), actor="tester", type="Other", asset_id=f"expb-{token}"
    )
    one = _incident(db, f"exp-one {token}", first.id)
    two = _incident(db, f"exp-two {token}", second.id)
    db.commit()
    client = _client()
    text = client.post("/incidents/export", data={"selected": [one.number, two.number]}).text
    assert "\t" not in text
    assert f"======== 1 of 2  {one.number} ========" in text
    assert f"======== 2 of 2  {two.number} ========" in text
    assert text.index(f"======== 1 of 2  {one.number} ========") < text.index(f"======== 2 of 2  {two.number} ========")
    lines = text.splitlines()
    id_at = [index for index, line in enumerate(lines) if line.startswith("id ")]
    assert len(id_at) == 2
    assert lines[id_at[0] - 1] == ""
    assert lines[id_at[1] - 1] == ""
    alone = client.post("/incidents/export", data={"selected": one.number}).text
    assert "======== 1 of 1" not in alone
    assert one.number in alone

    assets = client.post("/assets/export", data={"selected": [first.asset_id, second.asset_id]}).text
    assert "\t" not in assets
    assert f"======== 1 of 2  {first.hostname} ========" in assets
    assert f"======== 2 of 2  {second.hostname} ========" in assets
    assert assets.index(first.hostname) < assets.index(f"======== 2 of 2  {second.hostname} ========")

    report(db, "jobs", "sep", "ok", summary=f"journal-a {token}", detail="one")
    report(db, "jobs", "sep", "ok", summary=f"journal-b {token}", detail="two")
    rows = (
        db.query(JournalEntry)
        .filter(JournalEntry.summary.in_([f"journal-a {token}", f"journal-b {token}"]))
        .order_by(JournalEntry.id.asc())
        .all()
    )
    journal = client.post("/journal/export", data={"selected": [str(rows[0].id), str(rows[1].id)]}).text
    assert f"======== 1 of 2  journal-a {token} ========" in journal
    assert f"======== 2 of 2  journal-b {token} ========" in journal
    assert "\t" not in journal
    db.close()


def test_cache_bust_is_v08_26():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v08-26" in base
    assert "app.js?v=v08-26" in base
    assert "v08-25" not in base
