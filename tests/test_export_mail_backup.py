"""Vertical exports, incident time range, mail subject, ICMP squares, backup stamp and bulk delete."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.backup import INNER_ARCHIVE, backup_compact, list_archives
from app.db import Base, SessionLocal, engine
from app.history import incident_list_filters, list_history
from app.incident_report_mail import build_incident_report, incident_report_html
from app.inventory import create_manual_asset
from app.journal import report
from app.main import app
from app.models import AuditLog, Evidence, Incident, IncidentNote, Investigation, JournalEntry, User, utcnow
from app.notifications import build_escalation_html
from app.seed import seed
from app.security import hash_password
from app.services import ensure_notification, incident_mail_heading, next_incident_number, send_incident_report
from app.settings import settings

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


def _field_line(text: str, label: str) -> str:
    for line in text.splitlines():
        if line.startswith(label + " ") or line.startswith(label + "  "):
            return line
    return ""


def test_incident_journal_and_asset_exports_are_vertical_and_include_evidence():
    db = _db()
    token = uuid4().hex[:8]
    asset = create_manual_asset(
        db,
        hostname=f"sw-{token}",
        ip=f"10.81.{uuid4().int % 200 + 1}.9",
        actor="tester",
        type="Other",
        asset_id=f"exp-{token}",
        extras={"site": f"lab-{token}", "customer": "Acme"},
        notes=f"rack note {token}",
    )
    number = next_incident_number(db)
    row = Incident(
        number=number,
        title=f"Interface down {token}",
        severity="CRITICAL",
        status="OPEN",
        fingerprint=f"pretty:{token}",
        started_at=utcnow() - timedelta(hours=2),
        summary=f"Gi0/1 down {token}",
        alert_payload={"labels": {"alertname": f"IfDown{token}"}},
        asset_id=asset.id,
        ack_by=f"ack-{token}@forgesre.local",
    )
    db.add(row)
    db.flush()
    other = next_incident_number(db)
    db.add(
        Incident(
            number=other,
            title=f"Second {token}",
            severity="WARNING",
            status="INVESTIGATING",
            fingerprint=f"pretty2:{token}",
            started_at=utcnow(),
            summary=f"second body {token}",
            asset_id=asset.id,
        )
    )
    db.add(
        Investigation(
            incident_id=row.id,
            summary=f"rca summary {token}",
            likely_cause=f"wal growth {token}",
            confidence=64,
            recommended_action=f"rotate logs {token}",
            result={"facts": [{"text": f"fact line {token}"}]},
        )
    )
    db.add(
        IncidentNote(incident_id=row.id, actor=f"ops-{token}", body=f"cleaned wal {token}")
    )
    db.add(
        AuditLog(
            actor=f"noc-{token}@forgesre.local",
            action="incident.note",
            object_type="incident",
            object_id=number,
            data={"note": f"did the thing {token}"},
        )
    )
    db.add(
        Evidence(
            incident_id=row.id,
            kind="prometheus",
            title=f"Disk sample {token}",
            source="prometheus",
            evidence_id=f"ev-{token}",
            payload={"content": f"used 94 percent {token}"},
        )
    )
    db.commit()
    client = _client()
    exported = client.post("/incidents/export", data={"selected": [number, other]})
    assert exported.status_code == 200
    text = exported.text
    assert "\t" not in text
    assert "id\thost" not in text
    lines = text.splitlines()
    id_at = [i for i, line in enumerate(lines) if line.startswith("id ")]
    assert len(id_at) == 2
    assert lines[id_at[0] - 1] == ""
    assert lines[id_at[1] - 1] == ""
    assert number in _field_line(text, "id")
    assert f"sw-{token}" in _field_line(text, "hostname")
    assert f"Interface down {token}" in _field_line(text, "incident")
    assert "OPEN" in _field_line(text, "status")
    for needle in (
        "Who to call",
        "What happened",
        f"Gi0/1 down {token}",
        f"rca summary {token}",
        f"wal growth {token}",
        f"fact line {token}",
        "Who did what",
        f"noc-{token}@forgesre.local",
        f"did the thing {token}",
        "Operator notes",
        f"cleaned wal {token}",
        "Engineer evidence",
        f"Disk sample {token}",
        f"used 94 percent {token}",
        f"lab-{token}",
    ):
        assert needle in text, needle
    host_line = _field_line(text, "hostname")
    assert "OPEN" not in host_line
    assert asset.ip not in host_line

    assets = client.post("/assets/export", data={"selected": asset.asset_id}).text
    assert "\t" not in assets
    assert f"sw-{token}" in _field_line(assets, "hostname")
    assert asset.ip not in _field_line(assets, "hostname")
    assert asset.ip in _field_line(assets, "ip")
    assert f"rack note {token}" in assets
    assert "Acme" in assets
    assert not any(line.count(",") > 6 and asset.asset_id in line and asset.ip in line and f"sw-{token}" in line for line in assets.splitlines())

    report(db, "jobs", "pretty", "ok", summary=f"journal pretty {token}", detail=f"line one {token}\nline two {token}")
    entry = db.query(JournalEntry).filter(JournalEntry.summary == f"journal pretty {token}").one()
    journal = client.post("/journal/export", data={"selected": str(entry.id)}).text
    assert "\t" not in journal
    assert "jobs" in _field_line(journal, "module")
    assert f"journal pretty {token}" in _field_line(journal, "summary")
    assert f"line two {token}" in journal
    assert "when\tmodule" not in journal
    db.close()


def test_incidents_time_window_and_custom_range_keep_site_filter():
    db = _db()
    token = uuid4().hex[:6]
    site = f"range-{token}"
    now = datetime.now(timezone.utc)
    zone = ZoneInfo(settings.timezone or "UTC")
    specs = [
        ("recent", now - timedelta(minutes=20)),
        ("two-hours", now - timedelta(hours=2)),
        ("two-days", now - timedelta(days=2)),
        ("ten-days", now - timedelta(days=10)),
        ("forty-days", now - timedelta(days=40)),
    ]
    titles = {}
    for name, started in specs:
        host = create_manual_asset(
            db,
            hostname=f"{name}-{token}",
            ip=f"10.82.{uuid4().int % 200 + 1}.{uuid4().int % 200 + 1}",
            actor="tester",
            type="Other",
            asset_id=f"rng-{name}-{token}"[:32],
            extras={"site": site, "customer": "RangeCo"},
        )
        title = f"{name} incident {token}"
        titles[name] = title
        db.add(
            Incident(
                number=next_incident_number(db, started),
                title=title,
                severity="WARNING",
                status="OPEN",
                fingerprint=f"range:{name}:{token}",
                started_at=started,
                summary=name,
                asset_id=host.id,
            )
        )
        db.flush()
    db.commit()
    client = _client()
    page = client.get("/incidents")
    assert page.status_code == 200
    form = page.text.split('class="list-filters incidents-filters"', 1)[1].split("</form>", 1)[0]
    assert 'name="window"' in form
    assert "Last 1 hour" in form and "Last 6 hours" in form and "Last 24 hours" in form
    assert "Last 7 days" in form and "Last 30 days" in form and "Custom range" in form
    assert 'name="from"' in form and 'name="to"' in form and 'type="date"' in form
    assert 'name="status"' in form and 'name="site"' in form

    def listed(query: str) -> str:
        response = client.get(f"/incidents?site={site}&{query}")
        assert response.status_code == 200
        return response.text

    hour = listed("window=1h")
    assert titles["recent"] in hour
    assert titles["two-hours"] not in hour
    six = listed("window=6h")
    assert titles["recent"] in six and titles["two-hours"] in six
    assert titles["two-days"] not in six
    day = listed("window=24h")
    assert titles["two-hours"] in day and titles["two-days"] not in day
    week = listed("window=7d")
    assert titles["two-days"] in week and titles["ten-days"] not in week
    month = listed("window=30d")
    assert titles["ten-days"] in month and titles["forty-days"] not in month

    local_day = (now - timedelta(days=2)).astimezone(zone).date().isoformat()
    custom = listed(f"window=custom&from={local_day}&to={local_day}")
    assert titles["two-days"] in custom
    assert titles["recent"] not in custom
    assert titles["ten-days"] not in custom
    assert f"window=custom" in custom and f"from={local_day}" in custom

    swapped = incident_list_filters(window="custom", started_from="2026-10-08", started_to="2026-10-01")
    assert swapped["date_from"] == "2026-10-01"
    assert swapped["date_to"] == "2026-10-08"
    assert swapped["query"]["since"] < swapped["query"]["until"]
    legacy = incident_list_filters(days="1")
    assert legacy["query"]["days"] == 1 and legacy["query"]["since"] is None
    preset = incident_list_filters(window="6h", days="30")
    assert preset["query"]["days"] is None and preset["query"]["since"] is not None
    rows, total = list_history(db, site=site, **{k: v for k, v in preset["query"].items() if k != "site"})
    assert total >= 1
    assert all(item.title.endswith(token) for item in rows)
    db.close()


def test_email_subject_is_hostname_then_problem_and_footer_is_gone():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(
        db,
        hostname=f"mail-{token}",
        ip=f"10.83.{uuid4().int % 200 + 1}.4",
        actor="tester",
        type="Other",
        owner_email="ops@dc.local",
    )
    incident = Incident(
        number=next_incident_number(db),
        title=f"CPU hot {token}",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"mailsub:{token}",
        summary="cpu",
        asset_id=asset.id,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    heading = incident_mail_heading(incident)
    assert heading.startswith(f"mail-{token} — ")
    assert f"CPU hot {token}" in heading
    assert incident.number not in heading
    note = ensure_notification(db, incident, "immediate")
    assert note.subject == heading
    assert note.subject.index(f"mail-{token}") < note.subject.index(f"CPU hot {token}")
    html = build_escalation_html(incident, "immediate", "team")
    assert heading in html
    assert "does not execute playbooks" not in html
    assert "This is a snapshot" not in html
    plain = build_incident_report(db, incident)
    report_html = incident_report_html(incident, db)
    assert "does not execute playbooks" not in plain
    assert "This is a snapshot" not in plain
    assert heading in report_html
    sent = send_incident_report(db, incident, "ops@dc.local", actor="admin@forgesre.local")
    assert sent.subject == heading

    from sqlalchemy.orm import joinedload

    from app.models import Asset

    demo_asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    demo_row = (
        db.query(Incident)
        .options(joinedload(Incident.asset))
        .filter(Incident.asset_id == demo_asset.id)
        .order_by(Incident.id.desc())
        .first()
    )
    assert demo_row is not None and demo_row.asset is not None
    demo_heading = incident_mail_heading(demo_row)
    demo_note = ensure_notification(db, demo_row, "15m")
    assert demo_note.subject.startswith("[DEMO] ")
    assert demo_heading in demo_note.subject
    assert demo_row.asset.hostname in demo_note.subject
    assert demo_row.title in demo_note.subject
    assert demo_note.subject.index(demo_row.asset.hostname) < demo_note.subject.index(demo_row.title)
    db.close()


def test_asset_markers_are_stacked_squares_without_a_colon_prefix():
    db = _db()
    client = _client()
    page = client.get("/assets").text
    assert "reach-stack" in page and "reach-sq icmp" in page and ">ICMP<" in page
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    block = css.split(".reach-sq {", 1)[1].split("}", 1)[0]
    assert "border-radius: 2px" in block
    assert "999px" not in block
    stack_css = css.split(".reach-stack {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in stack_css
    assert "gap:" in stack_css
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'ping.textContent = "ICMP"' in js
    assert 'snmpEl.textContent = "SNMP"' in js
    db.close()


def test_backup_row_uses_compact_stamp_and_delete_selected_is_admin_only(tmp_path, monkeypatch):
    db = _db()
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    name = "backup_20261008T143000Z"
    folder = tmp_path / "backups" / name
    folder.mkdir(parents=True)
    (folder / INNER_ARCHIVE).write_bytes(b"x" * 32)
    keep = tmp_path / "backups" / "backup_20261007T100000Z"
    keep.mkdir()
    (keep / INNER_ARCHIVE).write_bytes(b"y" * 32)
    client = _client()
    page = client.get("/admin")
    assert page.status_code == 200
    text = page.text
    compact = backup_compact(next(row for row in list_archives() if row["name"] == name))
    assert compact["date"] and compact["time"]
    backups = text.split("<h2>Platform backup", 1)[1].split("admin-backup-cli", 1)[0]
    assert compact["date"] in backups and compact["time"] in backups
    cell = backups.split('class="backup-when"', 1)[1].split("</td>", 1)[0]
    assert "stamp-date" in cell and "stamp-time" in cell
    assert f"<code>{name}</code>" not in cell
    assert name in cell
    assert "Delete selected" in backups
    assert 'action="/admin/backups/bulk-delete"' in backups
    assert ">Download</a>" in backups and ">Remove<" in backups
    audit = text.split("<h2>Audit log</h2>", 1)[1].split("<h2>Platform backup", 1)[0]
    assert "Delete selected" not in audit
    removed = client.post("/admin/backups/bulk-delete", data={"selected": name}, follow_redirects=False)
    assert removed.status_code == 303
    assert not folder.exists()
    assert keep.is_dir()
    db.add(User(email="ana-bak@dc.local", name="Ana", password_hash=hash_password("ana-pass"), role="analyst"))
    db.commit()
    analyst = _client("ana-bak@dc.local", "ana-pass")
    denied = analyst.post("/admin/backups/bulk-delete", data={"selected": keep.name}, follow_redirects=False)
    assert denied.status_code == 403
    assert keep.is_dir()
    db.close()
