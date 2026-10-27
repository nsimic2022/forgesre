from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.journal import KEEP_DAYS, KEEP_PER_MODULE, list_entries, next_error_ack_id, prune_module, prune_older_than, report
from app.main import app
from app.models import JournalEntry, utcnow
from app.seed import seed


def test_journal_report_and_module_split():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    report(db, "inventory", "asset.create", "ok", summary="Saved app-journal-01 (ops@dc.local)", object_id="app-journal-01")
    report(db, "rca", "investigate", "error", summary="Prometheus down", detail="connection refused")
    report(db, "netbox", "sync", "warn", summary="NetBox API slow")
    rows = list_entries(db, module="inventory")
    assert rows
    assert all(item.module == "inventory" for item in rows)
    found = list_entries(db, q="prometheus")
    assert any(item.status == "error" for item in found)
    assert any(item.module == "netbox" for item in list_entries(db, q="netbox"))
    assert any(item.status == "warn" for item in list_entries(db, q="warn"))
    assert any(item.status == "error" for item in list_entries(db, q="critical"))
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


def test_journal_redirects_to_health_and_nav_has_no_journal_item():
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    bounced = client.get("/journal?status=error&q=smtp", follow_redirects=False)
    assert bounced.status_code == 302
    assert bounced.headers["location"] == "/health-ui?status=error&q=smtp#journal"
    health = client.get("/health-ui")
    assert health.status_code == 200
    assert 'id="journal"' in health.text
    section = health.text.split('id="journal"', 1)[1]
    assert "Journal modules" not in section
    assert "any status" not in section
    assert 'placeholder="Search module, status, summary, netbox, warn' in section
    assert 'name="status"' not in section.split("<form", 1)[1].split("</form>", 1)[0]
    assert 'name="selected"' in section
    assert 'action="/journal/export"' in section
    assert 'action="/journal/bulk-delete"' in section
    assert 'class="pager-bar"' in section
    nav = health.text.split('<aside class="nav"', 1)[1].split("</aside>", 1)[0]
    assert ">Journal<" not in nav
    assert 'href="/journal"' not in nav
    assert 'href="/health-ui"' in nav


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
    assert b"Recent incidents" in home.content
    assert b"Recent journal" not in home.content
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
    assert KEEP_DAYS == 30


def test_journal_prunes_rows_older_than_30_days():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    old = JournalEntry(module="demo", action="old", status="ok", summary="stale row", at=utcnow() - timedelta(days=31))
    fresh = JournalEntry(module="demo", action="new", status="ok", summary="fresh row", at=utcnow())
    db.add_all([old, fresh])
    db.commit()
    assert prune_older_than(db, days=30) >= 1
    left = db.query(JournalEntry).filter(JournalEntry.module == "demo", JournalEntry.action.in_(["old", "new"])).all()
    assert [row.action for row in left] == ["new"]
    found = list_entries(db, q="critical")
    assert isinstance(found, list)
    db.close()


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
    assert b"Recent incidents" in home.content
    assert b"Recent journal" not in home.content
    assert b"SMTP send failed no-dash-banner" not in home.content
    health = client.get("/health-ui")
    assert health.status_code == 200
    assert b'SMTP send failed no-dash-banner' in health.content.split(b'id="journal"', 1)[1]
    assert client.post("/dashboard/journal-ack", data={"until_id": "1"}, follow_redirects=False).status_code in {404, 405}
