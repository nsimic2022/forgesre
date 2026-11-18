"""Background work is safe with several Core processes: atomic job claim, separate LLM lane,
one leader for the loops, readable `./forgesre jobs`."""

from __future__ import annotations

from uuid import uuid4

from app.db import Base, SessionLocal, engine
from app.jobs import LANE_DEFAULT, LANE_LLM, claim_job, job_counts, next_pending_job
from app.leader import LeaderLock
from app.models import Job
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _job(db, kind: str = "investigate", use_llm: bool = False) -> Job:
    row = Job(kind=kind, status="pending", object_type="incident", object_id=f"T-{uuid4().hex[:8]}", payload={"use_llm": use_llm})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _clear_pending(db) -> None:
    db.query(Job).filter(Job.status == "pending").update({"status": "done"}, synchronize_session=False)
    db.commit()


def test_a_job_can_be_claimed_only_once():
    db = _db()
    row = _job(db)
    other = SessionLocal()  # a second Core process
    try:
        same_row = other.get(Job, row.id)
        assert claim_job(db, row) is True
        assert claim_job(other, same_row) is False
        db.refresh(row)
        assert row.status == "running" and row.attempts == 1 and row.started_at is not None
    finally:
        other.close()
        db.close()


def test_llm_jobs_have_their_own_lane():
    db = _db()
    _clear_pending(db)
    rewrite = _job(db, use_llm=True)
    rca = _job(db, use_llm=False)
    assert next_pending_job(db, LANE_LLM).id == rewrite.id
    assert next_pending_job(db, LANE_DEFAULT).id == rca.id
    claim_job(db, rca)
    assert next_pending_job(db, LANE_DEFAULT) is None
    assert next_pending_job(db, LANE_LLM).id == rewrite.id
    _clear_pending(db)
    db.close()


def test_job_counts_by_status():
    db = _db()
    before = job_counts(db).get("error", 0)
    row = _job(db)
    row.status = "error"
    db.commit()
    assert job_counts(db)["error"] == before + 1
    db.close()


class _FakePg:
    """Stands in for a Postgres connection; `holder` says who owns the advisory lock."""

    holder: list = []

    def __init__(self):
        self.closed = False

    def cursor(self):
        conn = self

        class _Cur:
            def execute(self, sql, params=None):
                if "pg_try_advisory_lock" in sql:
                    if not _FakePg.holder or _FakePg.holder[0].closed:
                        _FakePg.holder[:] = [conn]
                    self.result = _FakePg.holder[0] is conn
                else:
                    if conn.closed:
                        raise RuntimeError("connection closed")
                    self.result = 1

            def fetchone(self):
                return (self.result,)

            def close(self):
                pass

        return _Cur()

    def close(self):
        self.closed = True


def test_only_one_process_leads_and_a_new_one_takes_over():
    _FakePg.holder = []
    first = LeaderLock(_FakePg, "postgresql")
    second = LeaderLock(_FakePg, "postgresql")
    assert first.ensure() is True
    assert second.ensure() is False
    assert first.ensure() is True  # keeps the lock on later rounds
    first.release()  # process exits: Postgres drops the lock with the connection
    assert second.ensure() is True
    assert first.ensure() is False


def test_sqlite_process_is_always_leader():
    assert LeaderLock(None, "sqlite").ensure() is True


def test_cli_jobs_output_shows_counts_and_errors():
    from app.cli_ops import format_jobs

    text = format_jobs(
        [
            {"id": 7, "status": "error", "kind": "investigate", "object_id": "INC-0007", "error": "LLM timeout after 120 s\nmore", "finished_at": "2026-11-18T10:00:00"},
            {"id": 6, "status": "done", "kind": "discovery_scan", "object_id": "scan", "finished_at": "2026-11-18T09:00:00"},
        ],
        {"pending": 2, "running": 1, "error": 1, "done": 40},
    )
    assert text.startswith("Jobs  pending 2  running 1  error 1  done 40")
    assert "#7" in text and "INC-0007" in text and "LLM timeout after 120 s" in text
    assert "more" not in text
