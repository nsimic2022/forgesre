"""Walk the whole UI as the admin: no page may answer 5xx, no link may 404, no form may
post to a route that does not exist. scripts/gui_crawl.py runs the same walk on a live VM."""

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

NAV = ["/", "/incidents", "/investigations", "/assets", "/discovery", "/playrules",
       "/playbooks", "/escalation", "/ops", "/health-ui", "/admin"]


def test_admin_can_walk_every_page_without_errors():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "instance": "forge-demo-01:9100", "severity": "high"},
                    "annotations": {"summary": "CPU high (crawl)"},
                    "startsAt": "2026-11-18T08:00:00Z",
                }
            ],
        },
    )
    db.commit()
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})

    report = gui_crawl.crawl(client, routes=gui_crawl.get_routes(app))

    assert not report.problems, "\n".join(p.line() for p in report.problems)
    for path in NAV:
        assert report.pages.get(path) == 200, f"menu page {path} not reached or not 200"
    assert any(u.startswith("/incidents/INC-") for u in report.pages), "incident detail page not reached"


def test_route_listing_sees_included_routers():
    routes = gui_crawl.get_routes(app)
    assert ("POST", "/logout") in routes
    assert ("POST", "/playrules/{rule_id}/toggle") in routes
    assert len(routes) > 150
