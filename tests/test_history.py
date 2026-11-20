
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(db, email="admin@forgesre.local", password="testpass"):
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


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


