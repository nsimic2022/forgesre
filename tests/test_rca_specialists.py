"""Named RCA specialists and the Investigations runs list."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.insights import list_investigation_runs
from app.main import app
from app.models import Incident
from app.seed import seed
from app.services import run_investigation
from rca.engines import ForgeRCA
from rca.specialists import specialist_brief, specialists_from_evidence

ROOT = Path(__file__).resolve().parents[1]


class _Row:
    def __init__(self, kind, content, metadata=None, evidence_id="EV-00001"):
        self.kind = kind
        self.payload = {"content": content, "metadata": metadata or {}}
        self.evidence_id = evidence_id


def test_specialists_from_metric_log_inventory_history():
    raw = specialists_from_evidence([
        _Row("METRIC", {"name": "cpu_percent", "value": 94, "unit": "percent"}),
        _Row("LOG", {"line": "oom-killer"}, {"scope": "syslog"}),
        _Row("INVENTORY", {"hostname": "sw-a", "type": "linux"}),
        _Row("INCIDENT_HISTORY", [{"number": "INC-1"}, {"number": "INC-2"}, {"number": "INC-3"}]),
    ])
    assert [item["id"] for item in raw] == ["inventory", "metrics", "logs", "history"]
    cubes = {item["id"]: item for item in raw}
    assert cubes["metrics"]["css"] == "warn"
    assert "CPU 94%" in cubes["metrics"]["detail"]
    assert cubes["logs"]["css"] == "ok"
    assert "oom-killer" in cubes["logs"]["detail"]
    assert cubes["inventory"]["css"] == "ok"
    assert "sw-a" in cubes["inventory"]["detail"]
    brief = specialist_brief([
        _Row("METRIC", {"name": "cpu_percent", "value": 94, "unit": "percent"}),
        _Row("METRIC", {"name": "net_rx", "value": 2_500_000, "unit": "bytes"}),
        _Row("LOG", {"message": "kernel: oom-killer"}, {"scope": "syslog"}),
        _Row("INVENTORY", {"hostname": "sw-a", "type": "switch", "source": "netbox"}),
        _Row("INCIDENT_HISTORY", [{"number": "INC-1", "title": "link down"}]),
        _Row("PLAYRULE", {"playbook": "IFACE-DOWN"}),
    ])
    facts = {item["id"]: item["facts"] for item in brief["workers"]}
    assert any("SNMP rx" in line for line in facts["metrics"])
    assert any("oom-killer" in line for line in facts["logs"])
    assert any("NetBox" in line for line in facts["inventory"])
    assert any("IFACE-DOWN" in line for line in facts["history"])
    assert cubes["history"]["css"] == "warn"
    assert cubes["history"]["detail"].startswith("3 earlier")


def test_specialists_empty_and_prom_down():
    empty = specialists_from_evidence([])
    assert [item["css"] for item in empty] == ["ignored"] * 4
    down = specialists_from_evidence([
        _Row("METRIC", {"error": "timeout"}, {"unavailable": True}),
    ])
    by_id = {item["id"]: item for item in down}
    assert [item["id"] for item in down] == ["inventory", "metrics", "logs", "history"]
    assert by_id["metrics"]["css"] == "crit"
    assert "Prometheus" in by_id["metrics"]["detail"]


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
    assert "case-specialists" in ai.text
    assert "specialist-row-tall" in ai.text
    assert ">Metrics<" in ai.text
    assert ">Logs<" in ai.text
    assert ">Inventory<" in ai.text
    assert ">History<" in ai.text
    incident = client.get(f"/incidents/{number}")
    assert incident.status_code == 200
    assert 'class="specialist-row"' in incident.text
    listing = client.get(f"/investigations?n={number}")
    assert listing.status_code == 200
    assert 'id="activity"' in listing.text
    activity = listing.text.split('id="activity"', 1)[1].split('id="summary"', 1)[0]
    assert "filter-chips" not in activity
    assert "Start from an" not in activity
    assert ">Investigation<" not in activity
    assert 'class="act-expand"' not in activity
    assert "case-specialists" in activity
    assert "specialist-row-tall" in activity
    assert ">Metrics<" in activity
    assert ">Logs<" in activity
    assert ">Inventory<" in activity
    assert ">History<" in activity
    assert number in listing.text
    runs = list_investigation_runs(db)
    match = next(row for row in runs if row["number"] == number)
    assert len(match["specialists"]) == 4
    assert match["writer"]["label"] == "ForgeAI"
    home = client.get("/").text
    cube = home.split('id="forgeai"', 1)[1].split("</section>", 1)[0]
    assert "Recent investigations" in cube
    assert "Recent activity" not in cube
    assert "Ask ForgeAI" not in cube
    assert "Open Investigations" not in cube
    assert f'href="/investigations?n={number}"' in cube
    assert 'class="act-case"' in cube
    assert cube.index(">Inventory<") < cube.index(">Metrics<") < cube.index(">Logs<") < cube.index(">History<")
    case = client.get(location).text
    header = case.split("<h1>", 1)[0]
    assert 'class="case-specialists' in header
    assert header.index(">Inventory<") < header.index(">Metrics<") < header.index(">Logs<") < header.index(">History<")
    db.close()


def test_writer_uses_specialist_brief_only():
    class _Writer:
        last_error = ""

        def __init__(self, payload):
            self.payload = payload
            self.user = ""
            self.system = ""

        def get_name(self):
            return "capturing"

        def get_model(self):
            return "local"

        def complete_json(self, system, user):
            self.system = system
            self.user = user
            return self.payload

    ctx = {
        "incident": {"number": "INC-9", "title": "High CPU", "asset": "sw-a", "severity": "warning"},
        "asset": {"hostname": "sw-a", "type": "linux", "source": "netbox"},
        "alert": {"alertname": "NodeCPUHigh"},
        "metrics": {"cpu_percent": 94},
        "logs": ["kernel: oom-killer"],
        "history": [{"number": "INC-1", "title": "old cpu"}],
    }
    writer = _Writer({"sentences": ["CPU is 94% on sw-a.", "Loki shows oom-killer.", "NetBox lists sw-a."]})
    result = ForgeRCA(llm=writer).investigate(ctx)
    builtin = ForgeRCA().investigate(ctx)
    assert "specialists" in writer.user
    assert "sentences" in writer.system
    assert result["provider"] == "forgerca-llm"
    assert result["summary"] == "CPU is 94% on sw-a. Loki shows oom-killer. NetBox lists sw-a."
    assert result["likely_cause"] == builtin["likely_cause"]
    assert result["recommended_action"] == builtin["recommended_action"]
    assert result["result"]["writer_sentences"] == [
        "CPU is 94% on sw-a.",
        "Loki shows oom-killer.",
        "NetBox lists sw-a.",
    ]
    lied = _Writer({"sentences": ["Memory is 12% on other-host."]})
    dropped = ForgeRCA(llm=lied).investigate(ctx)
    assert dropped["provider"] == "builtin-analyst"
    assert dropped["summary"] == builtin["summary"]


def test_cache_bust_is_v09_35():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "app.css?v=v09-57" in base
    assert ".specialist-row" in css
    assert ".specialist-cube.ok" in css
    assert ".act-expand" in css
    assert ".specialist-row-5" in css
    assert ".specialist-row-tall" in css
    assert ".act-case-name" in css
