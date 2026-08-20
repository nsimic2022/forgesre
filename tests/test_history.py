from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Incident, Notification, User
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
