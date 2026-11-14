"""Partial unique index on active incident fingerprints. RESOLVED may still refire."""

from __future__ import annotations

import threading
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.migrate import ACTIVE_FINGERPRINT_INDEX, migrate
from app.models import TRACKED_INCIDENT_STATUSES, Incident, Job, utcnow
from app.seed import seed
from app.services import _insert_open_incident, ingest_alertmanager, next_incident_number, tracked_incident_statuses


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _drop_pending_jobs(db) -> None:
    db.query(Job).filter(Job.status == "pending").delete(synchronize_session=False)
    db.commit()


def _alert(alertname: str, asset: str, status: str = "firing") -> dict:
    labels = {"alertname": alertname, "severity": "warning"}
    if asset:
        labels["asset"] = asset
    return {
        "status": status,
        "alerts": [{"status": status, "labels": labels, "annotations": {"summary": f"{alertname} on {asset}"}}],
    }


def _host(db, prefix: str):
    return create_manual_asset(
        db,
        hostname=f"{prefix}-{uuid4().hex[:6]}",
        ip="10.81.%d.%d" % (uuid4().int % 250 + 1, uuid4().int % 230 + 20),
        type="Linux Server",
        actor="tester",
    )


def _index_names(eng, table: str) -> set[str]:
    return {row["name"] for row in inspect(eng).get_indexes(table)}


def test_tracked_statuses_match_the_partial_index():
    assert tracked_incident_statuses() == TRACKED_INCIDENT_STATUSES


def test_migrate_collapses_duplicate_active_fingerprints_keeping_max_id(tmp_path):
    old = create_engine(f"sqlite:///{tmp_path / 'dup.db'}")
    Base.metadata.create_all(bind=old)
    with old.begin() as conn:
        conn.execute(text(f"DROP INDEX IF EXISTS {ACTIVE_FINGERPRINT_INDEX}"))
    SessionLocalOld = sessionmaker(bind=old)
    db = SessionLocalOld()
    fingerprint = f"DupCPU:{uuid4().hex[:8]}"
    older = Incident(
        number=f"INC-0100_01.01.2026_00:00",
        title="older",
        status="OPEN",
        fingerprint=fingerprint,
        timeline=[],
        alert_payload={},
        started_at=utcnow(),
    )
    newer = Incident(
        number=f"INC-0101_01.01.2026_00:01",
        title="newer",
        status="INVESTIGATING",
        fingerprint=fingerprint,
        timeline=[],
        alert_payload={},
        started_at=utcnow(),
    )
    resolved = Incident(
        number=f"INC-0099_01.01.2026_00:00",
        title="history",
        status="RESOLVED",
        fingerprint=fingerprint,
        timeline=[],
        alert_payload={},
        started_at=utcnow(),
        resolved_by="Alertmanager",
    )
    db.add_all([older, newer, resolved])
    db.commit()
    keep_id = newer.id
    older_id = older.id
    history_id = resolved.id
    db.close()

    migrate(old)
    migrate(old)
    assert ACTIVE_FINGERPRINT_INDEX in _index_names(old, "incidents")

    db = SessionLocalOld()
    keep = db.get(Incident, keep_id)
    collapsed = db.get(Incident, older_id)
    history = db.get(Incident, history_id)
    assert keep is not None and keep.status == "INVESTIGATING"
    assert collapsed is not None and collapsed.status == "RESOLVED"
    assert collapsed.resolved_by == "system"
    assert any(item.get("id") == "collapse" for item in (collapsed.timeline or []))
    assert history is not None and history.status == "RESOLVED"
    assert history.resolved_by == "Alertmanager"
    active = (
        db.query(Incident)
        .filter(Incident.fingerprint == fingerprint, Incident.status.in_(TRACKED_INCIDENT_STATUSES))
        .all()
    )
    assert [row.id for row in active] == [keep_id]
    db.close()
    old.dispose()


def test_insert_conflict_reuses_existing_active_row():
    db = _db()
    host = _host(db, "fp-reuse")
    fingerprint = f"ReuseCPU:{host.asset_id}"
    existing = Incident(
        number=next_incident_number(db),
        title="live",
        status="OPEN",
        fingerprint=fingerprint,
        timeline=[],
        alert_payload={},
        started_at=utcnow(),
    )
    db.add(existing)
    db.flush()
    live_id = existing.id
    duplicate = Incident(
        number=next_incident_number(db),
        title="race",
        status="OPEN",
        fingerprint=fingerprint,
        timeline=[],
        alert_payload={},
        started_at=utcnow(),
    )
    got = _insert_open_incident(db, duplicate)
    assert got is not None
    assert got.id == live_id
    assert (
        db.query(Incident)
        .filter(Incident.fingerprint == fingerprint, Incident.status.in_(TRACKED_INCIDENT_STATUSES))
        .count()
        == 1
    )
    db.close()


def test_refire_after_resolved_still_opens_a_new_incident():
    db = _db()
    host = _host(db, "fp-refire")
    first = ingest_alertmanager(db, _alert("HighCPU", host.asset_id))
    assert len(first) == 1
    original = first[0]
    assert ingest_alertmanager(db, _alert("HighCPU", host.asset_id, status="resolved")) == []
    db.refresh(original)
    assert original.status == "RESOLVED"
    again = ingest_alertmanager(db, _alert("HighCPU", host.asset_id))
    assert len(again) == 1
    fresh = again[0]
    assert fresh.id != original.id
    assert fresh.status in TRACKED_INCIDENT_STATUSES
    db.refresh(original)
    assert original.status == "RESOLVED"
    _drop_pending_jobs(db)
    db.close()


def test_concurrent_ingest_leaves_one_active_row():
    db = _db()
    host = _host(db, "fp-race")
    asset_id = host.asset_id
    db.close()
    payload = _alert(f"RaceCPU{uuid4().hex[:6]}", asset_id)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            ingest_alertmanager(session, payload)
        except BaseException as exc:
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    check = SessionLocal()
    fingerprint = f"{payload['alerts'][0]['labels']['alertname']}:{asset_id}"
    active = (
        check.query(Incident)
        .filter(Incident.fingerprint == fingerprint, Incident.status.in_(TRACKED_INCIDENT_STATUSES))
        .all()
    )
    assert len(active) == 1
    _drop_pending_jobs(check)
    check.close()


def test_fresh_schema_has_the_partial_unique_index(tmp_path):
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=fresh)
    assert ACTIVE_FINGERPRINT_INDEX in _index_names(fresh, "incidents")
    migrate(fresh)
    assert ACTIVE_FINGERPRINT_INDEX in _index_names(fresh, "incidents")
    fresh.dispose()
