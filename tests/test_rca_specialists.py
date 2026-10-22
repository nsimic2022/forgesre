"""Named RCA specialists and the Investigations runs list."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.insights import list_investigation_runs
from app.main import app
from app.models import Incident
from app.seed import seed
from app.services import run_investigation
from rca.specialists import specialists_from_evidence

ROOT = Path(__file__).resolve().parents[1]


class _Row:
    def __init__(self, kind, content, metadata=None, evidence_id="EV-00001"):
        self.kind = kind
        self.payload = {"content": content, "metadata": metadata or {}}
        self.evidence_id = evidence_id


def test_specialists_from_metric_log_inventory_history():
    cubes = {item["id"]: item for item in specialists_from_evidence([
        _Row("METRIC", {"name": "cpu_percent", "value": 94, "unit": "percent"}),
        _Row("LOG", {"line": "oom-killer"}, {"scope": "syslog"}),
        _Row("INVENTORY", {"hostname": "sw-a", "type": "linux"}),
        _Row("INCIDENT_HISTORY", [{"number": "INC-1"}, {"number": "INC-2"}, {"number": "INC-3"}]),
    ])}
    assert cubes["metrics"]["css"] == "warn"
    assert "CPU 94%" in cubes["metrics"]["detail"]
    assert cubes["logs"]["css"] == "ok"
    assert cubes["logs"]["detail"].startswith("1 syslog")
    assert cubes["inventory"]["css"] == "ok"
    assert "sw-a" in cubes["inventory"]["detail"]
    assert cubes["history"]["css"] == "warn"
    assert cubes["history"]["detail"].startswith("3 earlier")


def test_specialists_empty_and_prom_down():
    empty = specialists_from_evidence([])
    assert [item["css"] for item in empty] == ["ignored"] * 4
    down = specialists_from_evidence([
        _Row("METRIC", {"error": "timeout"}, {"unavailable": True}),
    ])
    assert down[0]["css"] == "crit"
    assert "Prometheus" in down[0]["detail"]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_investigations_page_lists_runs_and_specialist_cubes():
    db = _db()
    incident = db.query(Incident).filter(Incident.asset_id.isnot(None)).first()
    assert incident is not None
    run_investigation(db, incident, actor="admin@forgesre.local", force=True, use_llm=False)
    number = incident.number
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    location = f"/ai/{number}"
    ai = client.get(location)
    assert ai.status_code == 200
    assert 'class="specialist-row"' in ai.text
    assert ">Metrics<" in ai.text
    assert ">Logs<" in ai.text
    assert ">Inventory<" in ai.text
    assert ">History<" in ai.text
    incident = client.get(f"/incidents/{number}")
    assert incident.status_code == 200
    assert 'class="specialist-row"' in incident.text
    listing = client.get("/investigations")
    assert listing.status_code == 200
    assert 'id="runs"' in listing.text
    assert number in listing.text
    assert 'href="/ai/%s"' % number in listing.text
    runs = list_investigation_runs(db)
    assert any(row["number"] == number for row in runs)
    db.close()


def test_cache_bust_is_v09_33():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "app.css?v=v09-33" in base
    assert ".specialist-row" in css
    assert ".specialist-cube.ok" in css
