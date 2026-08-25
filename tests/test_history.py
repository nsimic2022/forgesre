from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset, Incident, Notification, User
from app.security import hash_password
from app.seed import seed
from app.services import next_incident_number


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(db, email="admin@forgesre.local", password="testpass"):
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


def test_history_page_lists_seeded_closed_incident():
    db = _db()
    client = _client(db)
    page = client.get("/history")
    assert page.status_code == 200
    assert "History" in page.text
    assert "INC-" in page.text
    assert "forge-demo-01" in page.text
    closed = client.get("/history?status=CLOSED")
    assert closed.status_code == 200
    assert "INC-" in closed.text
    by_asset = client.get("/history?asset=forge-demo-01")
    assert by_asset.status_code == 200
    assert "forge-demo-01" in by_asset.text
    db.close()


def test_history_default_window_excludes_old_rows():
    db = _db()
    old = Incident(
        number=next_incident_number(db),
        title="Ancient disk",
        severity="WARNING",
        status="CLOSED",
        fingerprint="old-disk:forge-demo-01",
        started_at=datetime.now(timezone.utc) - timedelta(days=120),
        ended_at=datetime.now(timezone.utc) - timedelta(days=119),
        summary="Outside the 90-day default.",
    )
    db.add(old)
    db.commit()
    number = old.number
    client = _client(db)
    default = client.get("/history")
    assert number not in default.text
    wide = client.get("/history?days=200")
    assert number in wide.text
    db.close()


def test_viewer_can_open_history():
    db = _db()
    email = "history-viewer@forgesre.local"
    if db.query(User).filter_by(email=email).first() is None:
        db.add(
            User(
                email=email,
                name="History viewer",
                password_hash=hash_password("testpass"),
                role="viewer",
            )
        )
        db.commit()
    client = _client(db, email=email)
    page = client.get("/history")
    assert page.status_code == 200
    assert "History" in page.text
    db.close()


def test_ack_resolve_and_operator_note_on_incident():
    db = _db()
    incident = Incident(
        number=next_incident_number(db),
        title="History note test",
        severity="WARNING",
        status="OPEN",
        fingerprint="history-note-test",
        summary="Isolated row for history UI tests.",
    )
    db.add(incident)
    db.flush()
    db.add(
        Notification(
            incident_id=incident.id,
            channel="email",
            target="platform@forgesre.local",
            subject="History note test",
            body="Owner mail body",
            status="generated",
            step_key="t0",
        )
    )
    db.commit()
    number = incident.number
    client = _client(db)
    ack = client.post(
        f"/incidents/{number}/status",
        data={"status": "INVESTIGATING"},
        follow_redirects=False,
    )
    assert ack.status_code == 302
    note = client.post(
        f"/incidents/{number}/notes",
        data={"body": "Cleaned WAL on the host."},
        follow_redirects=False,
    )
    assert note.status_code == 302
    empty = client.post(
        f"/incidents/{number}/notes",
        data={"body": "   "},
        follow_redirects=False,
    )
    assert empty.status_code == 400
    resolve = client.post(
        f"/incidents/{number}/status",
        data={"status": "CLOSED"},
        follow_redirects=False,
    )
    assert resolve.status_code == 302
    page = client.get(f"/incidents/{number}")
    assert page.status_code == 200
    assert "admin@forgesre.local" in page.text
    assert "Cleaned WAL on the host." in page.text
    assert "Owner mail body" in page.text
    assert "incident.status" in page.text or "Who did what" in page.text
    api = client.get(f"/api/v1/incidents/{number}")
    assert api.status_code == 200
    body = api.json()
    assert body["ack_by"] == "admin@forgesre.local"
    assert body["resolved_by"] == "admin@forgesre.local"
    assert body["notes"][0]["body"] == "Cleaned WAL on the host."
    assert body["notifications"][0]["target"] == "platform@forgesre.local"
    db.close()


def test_history_api_and_empty_api_note():
    db = _db()
    client = _client(db)
    listed = client.get("/api/v1/history?days=90")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["days"] == 90
    assert payload["total"] >= 1
    assert payload["incidents"]
    closed = client.get("/api/v1/history?status=CLOSED&asset=forge-demo-01")
    assert closed.status_code == 200
    assert closed.json()["total"] >= 1
    number = closed.json()["incidents"][0]["number"]
    bad = client.post(f"/api/v1/incidents/{number}/notes", json={"body": ""})
    assert bad.status_code == 400
    db.close()


def test_cli_help_documents_history():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    overview = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help"], text=True)
    assert "history" in overview
    help_text = subprocess.check_output(
        ["bash", str(root / "scripts/forgesre"), "help", "history"],
        text=True,
    )
    assert "90-day" in help_text
    assert "INC-" in help_text
    assert "./forgesre history --days 90" in help_text


def test_incident_action_buttons_follow_status_colors():
    db = _db()
    incident = Incident(
        number=next_incident_number(db),
        title="button colors",
        severity="WARNING",
        status="OPEN",
        fingerprint="button-colors",
    )
    db.add(incident)
    db.commit()
    number = incident.number
    client = _client(db)
    open_page = client.get(f"/incidents/{number}")
    assert open_page.status_code == 200
    assert 'value="INVESTIGATING" class="todo"' in open_page.text
    assert 'value="RESOLVED" class="todo"' in open_page.text
    assert 'value="CLOSED" class="todo"' in open_page.text
    assert 'class="todo">Run AI investigation' in open_page.text
    client.post(f"/incidents/{number}/status", data={"status": "INVESTIGATING"}, follow_redirects=False)
    acked = client.get(f"/incidents/{number}")
    assert 'value="INVESTIGATING" class="done"' in acked.text
    assert 'value="RESOLVED" class="todo"' in acked.text
    assert 'value="CLOSED" class="todo"' in acked.text
    client.post(f"/incidents/{number}/status", data={"status": "RESOLVED"}, follow_redirects=False)
    resolved = client.get(f"/incidents/{number}")
    assert 'value="INVESTIGATING" class="done"' in resolved.text
    assert 'value="RESOLVED" class="done"' in resolved.text
    assert 'value="CLOSED" class="todo"' in resolved.text
    client.post(f"/incidents/{number}/status", data={"status": "CLOSED"}, follow_redirects=False)
    closed = client.get(f"/incidents/{number}")
    assert 'value="INVESTIGATING" class="done"' in closed.text
    assert 'value="RESOLVED" class="done"' in closed.text
    assert 'value="CLOSED" class="done"' in closed.text
    rca = client.post(f"/incidents/{number}/investigate", follow_redirects=False)
    assert rca.status_code in {302, 303}
    after_rca = client.get(f"/incidents/{number}")
    assert 'class="done">Run AI investigation' in after_rca.text
    db.close()


def test_send_incident_report_to_address_book_email():
    db = _db()
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    incident = Incident(
        number=next_incident_number(db),
        title="Disk full",
        severity="CRITICAL",
        status="OPEN",
        fingerprint="incident-report-mail",
        asset_id=asset.id,
        summary="Demo host disk is full.",
    )
    db.add(incident)
    db.commit()
    number = incident.number
    client = _client(db)
    page = client.get(f"/incidents/{number}")
    assert page.status_code == 200
    assert "Send incident report" in page.text
    assert "Report outbox" in page.text
    assert "platform@forgesre.local" in page.text
    assert page.text.find("Acknowledge") < page.text.find('class="pill open"') or page.text.find("Acknowledge") < page.text.find(">OPEN<")
    posted = client.post(
        f"/incidents/{number}/mail",
        data={"target": "ops@dc.local"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"].endswith("#mail")
    db.expire_all()
    mail = (
        db.query(Notification)
        .filter_by(incident_id=incident.id, step_key="incident-report")
        .one()
    )
    assert mail.target == "ops@dc.local"
    assert mail.status == "generated"
    assert "not sent (email not configured)" in (mail.error or "")
    assert mail.incident_id == incident.id
    outbox = client.get(f"/incidents/{number}")
    assert "not sent (email not configured)" in outbox.text
    assert 'class="pill generated">generated</span>' in outbox.text
    assert number in mail.body
    assert "Disk full" in mail.body
    assert "ForgeRCA has not been run yet." in mail.body
    listed = client.get("/incidents")
    assert listed.status_code == 200
    assert "Reported to" in listed.text
    assert "ops@dc.local" in listed.text
    assert f'class="inc-crit" href="/incidents/{number}"' in listed.text
    assert 'class="inc-ok"' in listed.text
    client.post(f"/incidents/{number}/investigate", follow_redirects=False)
    again = client.post(
        f"/incidents/{number}/mail",
        data={"new_email": "oncall@dc.local"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    db.expire_all()
    rca_mail = (
        db.query(Notification)
        .filter_by(target="oncall@dc.local", step_key="incident-report")
        .one()
    )
    assert "Likely cause:" in rca_mail.body
    assert "ForgeRCA" in rca_mail.body
    db.close()


def test_viewer_cannot_send_incident_report():
    db = _db()
    email = "report-viewer@forgesre.local"
    if db.query(User).filter_by(email=email).first() is None:
        db.add(User(email=email, name="V", password_hash=hash_password("testpass"), role="viewer"))
        db.commit()
    incident = db.query(Incident).filter(Incident.number.startswith("INC-")).first()
    client = _client(db, email=email)
    posted = client.post(
        f"/incidents/{incident.number}/mail",
        data={"target": "nope@example.local"},
        follow_redirects=False,
    )
    assert posted.status_code == 403
    db.close()
