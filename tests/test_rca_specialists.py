"""Named RCA specialists and the Investigations runs list."""

from pathlib import Path


from app.db import Base, SessionLocal, engine
from app.seed import seed
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


