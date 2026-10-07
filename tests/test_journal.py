from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.journal import KEEP_PER_MODULE, list_entries, next_error_ack_id, prune_module, report
from app.main import app
from app.models import JournalEntry
from app.seed import seed


def test_journal_report_and_module_split():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    report(db, "inventory", "asset.create", "ok", summary="Saved app-journal-01 (ops@dc.local)", object_id="app-journal-01")
    report(db, "rca", "investigate", "error", summary="Prometheus down", detail="connection refused")
    rows = list_entries(db, module="inventory")
    assert rows
    assert all(item.module == "inventory" for item in rows)
    found = list_entries(db, q="prometheus")
    assert any(item.status == "error" for item in found)
    db.close()


def test_journal_prunes_old_rows_per_module():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for i in range(12):
        db.add(JournalEntry(module="demo", action="flood", status="ok", summary=f"row {i}"))
    db.commit()
    deleted = prune_module(db, "demo", keep=5)
    assert deleted >= 7
    left = db.query(JournalEntry).filter_by(module="demo").count()
    assert left == 5
    db.close()


def test_console_page_and_api():
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    page = client.get("/journal")
    assert page.status_code == 200
    assert b"Journal" in page.content
    home = client.get("/")
    assert b"Recent journal reports" in home.content
    data = client.get("/api/v1/journal").json()
    assert "entries" in data
    assert "modules" in data
    created = client.post(
        "/api/v1/journal",
        json={
            "module": "install",
            "action": "install",
            "status": "ok",
            "summary": "Install finished profile=standard port=8080",
        },
    )
    assert created.status_code == 200
    assert created.json()["module"] == "install"
    filtered = client.get("/api/v1/journal?module=install").json()
    assert any(item["action"] == "install" for item in filtered["entries"])


def test_keep_default_is_small():
    assert KEEP_PER_MODULE == 200


def test_next_error_ack_id_does_not_skip_unseen():
    assert next_error_ack_id(None, 0, [10, 9, 8]) == 10
    assert next_error_ack_id(9, 0, [10, 9, 8]) == 9
    assert next_error_ack_id(99, 4, [10, 9]) == 10
    assert next_error_ack_id(3, 7, [10]) == 7


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    login = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert login.status_code in {302, 303}


def test_dashboard_has_no_journal_error_banner():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    report(db, "notification", "smtp.send", "error", summary="SMTP send failed no-dash-banner")
    db.close()

    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    assert b'id="journal-error-banner"' not in home.content
    assert b"banner-short" not in home.content
    assert b"/dashboard/journal-ack" not in home.content
    journal = home.content.split(b"Recent journal reports", 1)[1]
    assert b"SMTP send failed no-dash-banner" in journal
    assert client.post("/dashboard/journal-ack", data={"until_id": "1"}, follow_redirects=False).status_code in {404, 405}
