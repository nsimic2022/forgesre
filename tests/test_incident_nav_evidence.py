"""Incident detail: ← Older / Newer → in the /incidents order, and Engineer evidence as a When / Source / Action list."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Evidence, Incident
from app.seed import seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "static" / "app.js"
CSS = ROOT / "frontend" / "static" / "app.css"
TAG = "incnav:"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _drop_rows():
    yield
    db = _db()
    ids = [row.id for row in db.query(Incident.id).filter(Incident.fingerprint.like(f"{TAG}%"))]
    if ids:
        db.query(Evidence).filter(Evidence.incident_id.in_(ids)).delete(synchronize_session=False)
        db.query(Incident).filter(Incident.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add(statuses: list[str]) -> list[str]:
    db = _db()
    numbers = []
    for i, status in enumerate(statuses):
        row = Incident(
            number=next_incident_number(db),
            title=f"Nav walk {i} {uuid4().hex[:4]}",
            severity="WARNING",
            status=status,
            fingerprint=f"{TAG}{uuid4().hex}",
            started_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        numbers.append(row.number)
    db.close()
    return numbers


def _nav(html: str) -> str:
    start = html.index("data-incident-nav")
    return html[html.rindex("<nav", 0, start) : html.index("</nav>", start) + len("</nav>")]


def _step(nav: str, which: str) -> tuple[str, str | None]:
    """('a', href) for a live link, ('span', None) when disabled."""
    m = re.search(r"<(a|span) [^>]*data-incident-" + which + r"[^>]*>", nav)
    assert m, which
    tag = m.group(0)
    href = re.search(r'href="([^"]*)"', tag)
    return m.group(1), href.group(1) if href else None


def _with_evidence(n_rca: int) -> str:
    (number,) = _add(["OPEN"])
    db = _db()
    incident = db.query(Incident).filter_by(number=number).one()
    db.add(
        Evidence(
            incident_id=incident.id,
            kind="metrics",
            title="Metrics",
            payload={"cpu_percent": 92.5, "queries": {"cpu_percent": "avg(rate(node_cpu_seconds_total[5m]))"}},
            evidence_id="ROLLUP-metrics",
        )
    )
    for i in range(n_rca):
        db.add(
            Evidence(
                incident_id=incident.id,
                kind="METRIC",
                title=f"METRIC EV-{i + 1:05d}",
                payload={"evidence_id": f"EV-{i + 1:05d}", "type": "METRIC", "content": {"value": 90 + i, "unit": "%"}},
                evidence_id=f"EV-{i + 1:05d}",
                source="prometheus",
                query=f'up{{instance="h{i}"}}',
                hash=uuid4().hex,
            )
        )
    db.commit()
    db.close()
    return number


def _evidence(html: str) -> str:
    start = html.index('id="evidence"')
    start = html.rindex("<section", 0, start)
    return html[start : html.index("</section>", start)]


def test_evidence_view_does_not_change_stored_payload():
    number = _with_evidence(1)
    db = _db()
    before = [(row.evidence_id, json.dumps(row.payload, sort_keys=True)) for row in db.query(Evidence).join(Incident).filter(Incident.number == number)]
    db.close()
    _client().get(f"/incidents/{number}")
    db = _db()
    after = [(row.evidence_id, json.dumps(row.payload, sort_keys=True)) for row in db.query(Evidence).join(Incident).filter(Incident.number == number)]
    db.close()
    assert sorted(before) == sorted(after)
