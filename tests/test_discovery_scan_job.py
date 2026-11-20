"""A network scan is a Postgres job; the probe stays off the HTTP thread."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.jobs import DISCOVERY_SCAN_KIND, enqueue_discovery_scan, run_pending_jobs
from app.main import app
from app.migrate import migrate
from app.models import Job, JournalEntry
from app.seed import seed
from app.settings import settings

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    return client


def _run_only(db, job: Job) -> None:
    """Run the queue with other tests' pending jobs parked, so only ``job`` runs."""
    others = db.query(Job).filter(Job.status == "pending", Job.id != job.id).all()
    for row in others:
        row.status = "parked"
    db.commit()
    try:
        run_pending_jobs(db)
    finally:
        for row in others:
            row.status = "pending"
        db.commit()


def test_run_pending_jobs_executes_scan_and_swallows_probe_errors(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise RuntimeError("SNMP GET exploded")

    monkeypatch.setattr("app.inventory.run_scan", boom)
    job = enqueue_discovery_scan(db, actor="admin@forgesre.local", saved=False)
    assert job is not None
    assert job.status == "pending"
    _run_only(db, job)
    db.refresh(job)
    assert called["n"] == 1
    assert job.status == "error"
    assert "SNMP GET exploded" in (job.error or "")
    entry = (
        db.query(JournalEntry)
        .filter_by(module="discovery", action="scan", status="error")
        .order_by(JournalEntry.id.desc())
        .first()
    )
    assert entry is not None
    assert "exploded" in (entry.detail or entry.summary or "")
    db.close()


def test_run_pending_jobs_marks_scan_done(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kwargs: {"found": 0, "skipped": 0, "cidrs": cidrs or []},
    )
    job = enqueue_discovery_scan(db, actor="t", cidrs=["10.1.0.0/28"], saved=True)
    _run_only(db, job)
    db.refresh(job)
    assert job.status == "done"
    assert (job.error or "") == ""
    db.close()


def test_api_scan_requires_saved_cidrs(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise RuntimeError("should not run on request")

    monkeypatch.setattr("app.inventory.run_scan", boom)
    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "cidrs", [])
    client = _login()
    empty = client.post("/api/v1/discovery/scan")
    assert empty.status_code == 400
    assert called["n"] == 0

    monkeypatch.setitem(settings.yaml.setdefault("discovery", {}), "cidrs", ["10.1.0.0/28"])
    resp = client.post("/api/v1/discovery/scan")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("queued") is True
    assert body.get("kind") == "discovery_scan"
    assert called["n"] == 0
    db = SessionLocal()
    _run_only(db, db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).order_by(Job.id.desc()).first())
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()
    assert called["n"] == 1


def test_job_worker_uses_operator_cidrs_without_remerge(monkeypatch):
    db = _db()
    seen = []

    def fake_run_scan(db, cidrs=None, merge_auto=True, **kwargs):
        seen.append({"cidrs": list(cidrs or []), "merge_auto": merge_auto})
        return {"found": 0, "skipped": 0, "cidrs": list(cidrs or []), "warnings": []}

    monkeypatch.setattr("app.inventory.run_scan", fake_run_scan)
    enqueue_discovery_scan(db, actor="t", cidrs=["10.20.30.0/25"], saved=True)
    _run_only(db, db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).order_by(Job.id.desc()).first())
    assert seen and seen[-1]["merge_auto"] is False
    assert seen[-1]["cidrs"] == ["10.20.30.0/25"]
    db.close()
