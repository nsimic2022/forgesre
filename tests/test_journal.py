from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.journal import KEEP_PER_MODULE, list_entries, prune_module, report
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
