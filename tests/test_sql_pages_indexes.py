"""/ops outbox and /admin audit page in SQL; migrate adds FK / started_at lookup indexes."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text

from app.db import Base, SessionLocal, engine
from app.main import app
from app.migrate import LOOKUP_INDEXES, migrate
from app.models import AuditLog, Notification
from app.seed import seed


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _capture_sql():
    seen: list[str] = []

    def _listen(conn, cursor, statement, params, context, executemany):
        seen.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", _listen)
    return seen, lambda: event.remove(engine, "before_cursor_execute", _listen)


def test_ops_outbox_pages_in_sql_and_shows_the_right_slice():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    tag = uuid4().hex[:6]
    for n in range(25):
        db.add(Notification(channel="email", target="noc@dc.local", subject=f"pg-{tag}-{n:02d}", body=f"body-{tag}-{n:02d}"))
    db.commit()
    client = _client()
    seen, stop = _capture_sql()
    try:
        page = client.get("/ops?per_page=10&page=2")
    finally:
        stop()
    assert page.status_code == 200
    mail = page.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    assert f"pg-{tag}-14" in mail and f"pg-{tag}-05" in mail
    assert f"pg-{tag}-24" not in mail and f"pg-{tag}-04" not in mail
    assert f"body-{tag}-24" not in page.text
    selects = [sql for sql in seen if sql.startswith("SELECT") and "NOTIFICATIONS.BODY" in sql and "COUNT(" not in sql]
    assert selects, seen
    assert all("LIMIT" in sql and "OFFSET" in sql for sql in selects), selects
    assert any("COUNT(" in sql and "FROM NOTIFICATIONS" in sql for sql in seen)
    db.close()


def test_admin_audit_pages_in_sql():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = _client()
    tag = uuid4().hex[:6]
    for n in range(15):
        db.add(AuditLog(actor="tester", action=f"pg.{tag}.{n:02d}", object_type="test", object_id=str(n)))
    db.commit()
    seen, stop = _capture_sql()
    try:
        page = client.get("/admin?audit_per_page=10&audit_page=1")
    finally:
        stop()
    assert page.status_code == 200
    assert f"pg.{tag}.14" in page.text and f"pg.{tag}.05" in page.text
    assert f"pg.{tag}.04" not in page.text
    selects = [sql for sql in seen if sql.startswith("SELECT") and "FROM AUDIT_LOG" in sql and "COUNT(" not in sql]
    assert selects and all("LIMIT" in sql for sql in selects), selects
    db.close()


def _index_names(eng, table: str) -> set[str]:
    return {row["name"] for row in inspect(eng).get_indexes(table)}


def test_migrate_adds_lookup_indexes_to_an_old_database(tmp_path):
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(bind=old)
    with old.begin() as conn:
        for table, column in LOOKUP_INDEXES:
            conn.execute(text(f"DROP INDEX IF EXISTS ix_{table}_{column}"))
    assert "ix_incidents_asset_id" not in _index_names(old, "incidents")
    migrate(old)
    migrate(old)
    for table, column in LOOKUP_INDEXES:
        assert f"ix_{table}_{column}" in _index_names(old, table), (table, column)
    wanted = {
        ("evidence", "incident_id"),
        ("investigations", "incident_id"),
        ("incident_events", "incident_id"),
        ("notifications", "incident_id"),
        ("incidents", "asset_id"),
        ("incidents", "started_at"),
    }
    assert wanted <= set(LOOKUP_INDEXES)
    old.dispose()


def test_fresh_schema_already_has_the_lookup_indexes(tmp_path):
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=fresh)
    for table, column in LOOKUP_INDEXES:
        assert f"ix_{table}_{column}" in _index_names(fresh, table), (table, column)
    fresh.dispose()
