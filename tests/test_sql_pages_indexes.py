"""/ops outbox and /admin audit page in SQL; migrate adds FK / started_at lookup indexes."""

from __future__ import annotations


from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text

from app.db import Base, engine
from app.main import app
from app.migrate import LOOKUP_INDEXES, migrate


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
