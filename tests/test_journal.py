import re

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.journal import KEEP_PER_MODULE, list_entries, next_error_ack_id, prune_module, report
from app.main import app
from app.models import JournalEntry, User
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


def _banner_until_id(html: bytes) -> int:
    match = re.search(rb'id="journal-error-banner".*?name="until_id" value="(\d+)"', html, re.S)
    assert match, html.decode()[:2000]
    return int(match.group(1))


def test_dashboard_journal_error_banner_ack_until_newer():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    first = report(db, "notification", "smtp.send", "error", summary="SMTP send failed banner-ack-1")
    second = report(db, "notification", "smtp.send", "error", summary="SMTP send failed banner-ack-2")
    report(db, "notification", "smtp.send", "warn", summary="SMTP warn must not count as error report")
    assert first and second
    first_id = int(first.id)
    second_id = int(second.id)
    db.close()
    assert second_id > first_id

    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    assert b'id="journal-error-banner"' in home.content
    assert b"recent error report" in home.content
    assert b"Open Journal" in home.content
    assert b">Dismiss</button>" in home.content
    until = _banner_until_id(home.content)
    assert until >= second_id

    ack = client.post(
        "/dashboard/journal-ack",
        data={"until_id": str(until)},
        follow_redirects=False,
    )
    assert ack.status_code in {302, 303}
    assert ack.headers.get("location") == "/"

    hidden = client.get("/")
    assert hidden.status_code == 200
    assert b'id="journal-error-banner"' not in hidden.content
    assert b"recent error report" not in hidden.content

    db = SessionLocal()
    admin = db.query(User).filter_by(email="admin@forgesre.local").one()
    assert int(admin.journal_error_ack_id or 0) == until
    report(db, "notification", "smtp.send", "warn", summary="SMTP warn after ack still not an error report")
    db.close()

    still_hidden = client.get("/")
    assert b'id="journal-error-banner"' not in still_hidden.content

    other = TestClient(app)
    _login(other)
    assert b'id="journal-error-banner"' not in other.get("/").content

    db = SessionLocal()
    newer = report(db, "notification", "smtp.send", "error", summary="SMTP send failed banner-ack-3")
    assert newer
    newer_id = int(newer.id)
    db.close()
    assert newer_id > second_id

    shown = client.get("/")
    assert shown.status_code == 200
    assert b'id="journal-error-banner"' in shown.content
    assert b"1 recent error report" in shown.content
    assert _banner_until_id(shown.content) >= newer_id
    assert b">Dismiss</button>" in shown.content
    assert b"Open Journal" in shown.content
