from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.journal import KEEP_DAYS, KEEP_PER_MODULE, list_entries, next_error_ack_id, prune_module, prune_older_than, report
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


