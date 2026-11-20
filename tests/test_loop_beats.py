"""Background loops report in the database, so any web or worker process can tell whether they run."""

from __future__ import annotations

from datetime import timedelta

from app import loop_beats
from app.db import Base, SessionLocal, engine
from app.models import LoopBeat, utcnow


def test_beats_are_shared_and_late_loops_warn(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setenv("FORGESRE_ROLE", "worker")
    loop_beats._last.clear()
    for name in loop_beats.EXPECTED:
        loop_beats.beat(name)
    loop_beats.beat("jobs")  # within MIN_GAP: no second write
    db = SessionLocal()
    row = db.get(LoopBeat, "jobs")
    assert row.process.startswith("worker@")
    assert loop_beats.age_seconds("jobs") < 5
    status = loop_beats.status()
    assert status["status"] == "ok" and "worker@" in status["why"]
    row.at = utcnow() - timedelta(minutes=10)
    db.commit()
    late = loop_beats.status()
    assert late["status"] == "warn" and "jobs" in late["why"]
    db.close()


def test_roles_decide_who_runs_the_loops(monkeypatch):
    from app.main import process_role, runs_loops

    for role, loops in (("all", True), ("web", False), ("worker", True), ("nonsense", True)):
        monkeypatch.setenv("FORGESRE_ROLE", role)
        assert runs_loops() is loops, role
    monkeypatch.setenv("FORGESRE_ROLE", "WEB")
    assert process_role() == "web"


def test_web_role_starts_no_loop_threads(monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FORGESRE_ROLE", "web")
    before = {t.name for t in threading.enumerate()}
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/health").status_code == 200
        started = {t.name for t in threading.enumerate()} - before
    assert not started & {"escalation", "jobs", "llm-jobs", "discovery"}


def test_doctor_has_a_background_loops_row():
    from app.api import _worker_check
    from app.stack import component_label

    assert component_label("worker") == "Background loops"
    assert _worker_check()["status"] in {"ok", "warn"}
