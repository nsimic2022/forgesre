"""Incident syslog is the asset's Loki lines, never the demo host on a real asset."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import SYSLOG_LIMIT, incident_syslog_lines
from app.inventory import create_manual_asset
from app.main import app
from app.models import Incident, utcnow
from app.seed import seed
from app.services import ingest_alertmanager


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_real_host_drops_demo_lines_and_queries_syslog():
    db = _db()
    token = uuid4().hex[:8]
    raw = uuid4().int
    asset = create_manual_asset(
        db,
        hostname=f"db-{token}",
        asset_id=f"db-{token}",
        ip=f"10.80.{raw % 200 + 1}.{(raw >> 8) % 200 + 1}",
        type="Linux Server",
        owner_email="db@dc.local",
        actor="tester",
    )
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": f"SyslogProbe{token}",
                        "severity": "warning",
                        "asset": asset.asset_id,
                    },
                    "annotations": {"summary": "probe"},
                }
            ],
        },
    )
    incident = created[0]
    seen: dict = {}

    def fetch(*, limit, query, start, end):
        seen["limit"] = limit
        seen["query"] = query
        seen["start"] = start
        seen["end"] = end
        return ["forge-demo-01 kernel oops", "Gi0/1 link down on db"]

    lines = incident_syslog_lines(incident, fetch=fetch)
    assert lines == ["Gi0/1 link down on db"]
    assert seen["limit"] == SYSLOG_LIMIT == 20
    assert 'job="syslog"' in seen["query"]
    assert f'hostname="{asset.hostname}"' in seen["query"]
    assert 'job="forgesre"' not in seen["query"]
    assert "forge-demo-01" not in seen["query"]
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=seen["start"].tzinfo)
    assert seen["start"] <= started <= seen["end"]
    assert seen["end"] - seen["start"] == timedelta(minutes=60)
    assert incident_syslog_lines(incident, fetch=lambda **_k: ["forge-demo-01 only"]) is None
    bare = Incident(number="INC-x", title="x", started_at=utcnow())
    assert incident_syslog_lines(bare, fetch=fetch) is None
    db.close()


def test_syslog_lines_render_and_empty_is_omitted(monkeypatch):
    db = _db()
    token = uuid4().hex[:8]
    raw = uuid4().int
    asset = create_manual_asset(
        db,
        hostname=f"edge-{token}",
        asset_id=f"edge-{token}",
        ip=f"10.81.{raw % 200 + 1}.{(raw >> 8) % 200 + 1}",
        type="Linux Server",
        owner_email="edge@dc.local",
        actor="tester",
    )
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": f"SyslogView{token}",
                        "severity": "warning",
                        "asset": asset.asset_id,
                    },
                    "annotations": {"summary": "view"},
                }
            ],
        },
    )
    number = created[0].number
    client = _client()
    monkeypatch.setattr("app.web.incident_syslog_lines", lambda _item: ["collector-line-gi01"])
    page = client.get(f"/incidents/{number}")
    assert page.status_code == 200
    block = page.text.split('data-syslog', 1)[1].split("</section>", 1)[0]
    assert "collector-line-gi01" in block
    assert "forge-demo-01" not in block
    monkeypatch.setattr("app.web.incident_syslog_lines", lambda _item: None)
    quiet = client.get(f"/incidents/{number}")
    assert 'data-syslog' not in quiet.text
    assert "forge-demo-01" not in quiet.text
    db.close()
