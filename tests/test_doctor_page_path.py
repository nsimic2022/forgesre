"""Doctor probes stay off list pages; /health-ui and dashboard still run them."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _clear_doctor():
    import app.api as api_mod

    api_mod._doctor_cache["at"] = 0.0
    api_mod._doctor_cache["payload"] = None
    api_mod._doctor_in_flight = None


def test_assets_page_does_not_run_fresh_doctor(monkeypatch):
    import app.api as api_mod

    _db().close()
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return {"overall": "HEALTHY", "components": {}, "failed": []}

    monkeypatch.setattr(api_mod, "_doctor_payload_fresh", counting)
    _clear_doctor()
    client = _login()
    assert client.get("/assets").status_code == 200
    assert calls["n"] == 0


def test_health_ui_still_runs_doctor(monkeypatch):
    import app.api as api_mod

    _db().close()
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return {
            "overall": "HEALTHY",
            "components": {"core": {"status": "ok", "why": ""}},
            "failed": [],
        }

    monkeypatch.setattr(api_mod, "_doctor_payload_fresh", counting)
    _clear_doctor()
    client = _login()
    assert client.get("/health-ui").status_code == 200
    assert calls["n"] == 1


def test_dashboard_runs_doctor_once(monkeypatch):
    import app.api as api_mod

    _db().close()
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return {
            "overall": "HEALTHY",
            "components": {"core": {"status": "ok", "why": ""}},
            "failed": [],
        }

    monkeypatch.setattr(api_mod, "_doctor_payload_fresh", counting)
    _clear_doctor()
    client = _login()
    assert client.get("/").status_code == 200
    assert calls["n"] == 1


def test_overlapping_doctor_force_shares_one_probe(monkeypatch):
    import app.api as api_mod

    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def slow():
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return {"overall": "HEALTHY", "components": {}, "failed": []}

    monkeypatch.setattr(api_mod, "_doctor_payload_fresh", slow)
    _clear_doctor()

    def run():
        api_mod.doctor_payload(force=True)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert calls["n"] == 1
