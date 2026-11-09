"""Failed background webhook ingest must leave a journal row."""

from __future__ import annotations

from app.api import _background_verify_and_route
from app.models import JournalEntry
from app.seed import seed


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_background_ingest_failure_writes_journal(monkeypatch):
    db = _db()

    def boom(*args, **kwargs):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr("app.services.ingest_alertmanager", boom)
    _background_verify_and_route({"status": "firing", "alerts": []}, "prometheus")
    rows = (
        db.query(JournalEntry)
        .filter_by(module="incident", action="ingest", status="error")
        .all()
    )
    assert any("ingest exploded" in (row.detail or "") for row in rows)
    assert any("prometheus" in (row.summary or "") for row in rows)
    db.close()
