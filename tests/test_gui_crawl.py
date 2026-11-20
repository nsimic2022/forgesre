"""Walk the app as the admin: every page and API read answers without 5xx, the app only calls API
paths that exist, and the old classic addresses lead into the app. scripts/gui_crawl.py runs the same
walk on a live VM."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed
from app.services import ingest_alertmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gui_crawl  # noqa: E402


def test_admin_can_walk_every_page_and_read_without_errors():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    ingest_alertmanager(db, {"status": "firing", "alerts": [{
        "status": "firing", "labels": {"alertname": "HighCPU", "instance": "forge-demo-01:9100", "severity": "high"},
        "annotations": {"summary": "CPU high (crawl)"}, "startsAt": "2026-11-18T08:00:00Z"}]})
    db.commit()
    db.close()
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})

    report = gui_crawl.crawl(client, routes=gui_crawl.get_routes(app))

    assert not report.problems, "\n".join(p.line() for p in report.problems)
    assert len([u for u in report.pages if u.startswith("/api/v1/")]) > 50
    assert any(u.startswith("/api/v1/incidents/INC-") for u in report.pages), "incident reads not reached"


def test_the_app_source_only_calls_existing_routes():
    paths = gui_crawl.frontend_api_paths()
    assert "/api/v1/noc" in paths and len(paths) > 50
    routes = gui_crawl.get_routes(app)
    assert [p for p in paths if not gui_crawl._known(p, routes)] == []
