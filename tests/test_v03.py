from rca.analysis import candidate_causes, detect_anomalies, facts_from, score_confidence
from rca.collector import collect_evidence_set
from rca.engines import DISCLAIMER, ForgeRCA, get_engine
from rca.llm import (
    PROMPT_CONTEXT_MAX_CHARS,
    NullLLM,
    extract_json,
    make_provider,
    prompt_context,
    validate_recommendation,
)
from rca.sanitize import sanitize
from rca.types import RCAContext, normalize_log, normalize_metric


def test_normalize_metric_and_log():
    metric = normalize_metric("cpu_usage", "94.2", "t", "percent")
    assert metric["type"] == "metric"
    assert metric["value"] == 94.2
    log = normalize_log("ERROR disk is filling", "t")
    assert log["severity"] == "ERROR"


def test_sanitize_secrets():
    raw = {
        "incident": {"title": "disk"},
        "NETBOX_API_TOKEN": "super-secret",
        "headers": {"Authorization": "Bearer abcdef"},
        "nested": {"smtp_password": "pw", "ok": "keep"},
    }
    cleaned = sanitize(raw)
    assert cleaned["NETBOX_API_TOKEN"] == "[REDACTED]"
    assert "[REDACTED]" in cleaned["headers"]["Authorization"]
    assert cleaned["nested"]["smtp_password"] == "[REDACTED]"
    assert cleaned["nested"]["ok"] == "keep"


def _fat_prom_matrix(series: int = 16, samples: int = 400) -> dict:
    """Prometheus query_range-shaped dump (labels + values: [[ts, x], …])."""
    ts0 = 1_700_000_001
    values = [[ts0 + i * 15, str(round(90.0 + (i % 10) * 0.1, 1))] for i in range(samples)]
    result = []
    for cpu in range(series):
        result.append(
            {
                "metric": {
                    "__name__": "node_cpu_seconds_total",
                    "cpu": str(cpu),
                    "mode": "idle",
                    "instance": "10.0.0.5:9100",
                    "job": "node",
                },
                "values": list(values),
            }
        )
    return {
        "status": "success",
        "data": {"resultType": "matrix", "result": result},
    }


def test_prompt_context_strips_prom_dump_keeps_cpu_mem():
    fat = _fat_prom_matrix()
    context = {
        "incident": {"number": "INC-1", "title": "High CPU", "severity": "warning", "smtp_password": "secret"},
        "asset": {"hostname": "app-01", "type": "Linux Server", "api_token": "tok-123"},
        "alerts": [{"alertname": "NodeCPUHigh"}],
        "metrics": {
            "cpu_percent": 92.0,
            "memory_percent": 81.0,
            "disk_percent": 74.0,
            "raw": fat,
        },
        "evidence": [
            {
                "evidence_id": "EV-00001",
                "type": "METRIC",
                "hash": "abc" * 10,
                "query": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
                "content": {"type": "metric", "name": "cpu_percent", "value": 92.0, "unit": "percent"},
            },
            {
                "evidence_id": "EV-00002",
                "type": "METRIC",
                "content": {"type": "metric", "name": "memory_percent", "value": 81.0, "unit": "percent"},
            },
            {
                "evidence_id": "EV-00003",
                "type": "METRIC",
                "content": {"type": "metric", "name": "disk_percent", "value": 74.0, "unit": "percent"},
            },
            {
                "evidence_id": "EV-00004",
                "type": "METRIC",
                "content": fat,
            },
            {
                "evidence_id": "EV-00005",
                "type": "LOG",
                "content": {"type": "log", "severity": "ERROR", "message": "kernel: oom-killer"},
            },
        ],
        "limitations": ["No host logs shipped."],
        "facts": [
            {"id": "fact-cpu", "text": "cpu_percent is 92.0%.", "evidence_ids": ["EV-00001"]},
            {"id": "fact-mem", "text": "memory_percent is 81.0%.", "evidence_ids": ["EV-00002"]},
        ],
    }
    payload = prompt_context(context)
    assert len(payload) <= PROMPT_CONTEXT_MAX_CHARS
    assert PROMPT_CONTEXT_MAX_CHARS < 12000
    assert "1700000001" not in payload
    assert "node_cpu_seconds_total" not in payload
    assert '"values"' not in payload
    assert "[[170" not in payload
    assert "abcabc" not in payload
    assert "secret" not in payload
    assert "tok-123" not in payload
    lowered = payload.lower()
    assert "92" in payload
    assert "81" in payload
    assert "74" in payload
    assert "cpu" in lowered
    assert "memory" in lowered or "mem" in lowered
    assert "disk" in lowered
    assert "oom-killer" in payload or "ERROR" in payload


class _CapturingLLM:
    last_error = ""

    def __init__(self) -> None:
        self.user = ""
        self.system = ""

    def get_name(self) -> str:
        return "capturing"

    def get_model(self) -> str:
        return "local"

    def complete_json(self, system: str, user: str):
        self.system = system
        self.user = user
        return None


def test_forgerca_llm_payload_is_compact_builtin_facts_stay():
    fat = _fat_prom_matrix()
    llm = _CapturingLLM()
    result = ForgeRCA(llm=llm).investigate(
        {
            "incident": {"number": "INC-1042", "title": "High CPU", "asset": "app-01", "severity": "warning"},
            "asset": {"hostname": "app-01", "type": "Linux Server"},
            "alert": {"alertname": "NodeCPUHigh"},
            "metrics": {
                "cpu_percent": 92,
                "memory_percent": 81,
                "disk_percent": 74,
                "raw": fat,
            },
            "logs": ["ERROR kernel: oom-killer"],
            "history": [],
        }
    )
    packed = result["result"]
    fact_blob = " ".join(str(item.get("text") or "") for item in packed["facts"])
    assert "92" in fact_blob
    assert "81" in fact_blob or "memory" in fact_blob.lower()
    assert packed["hypotheses"]
    assert packed["facts"]
    assert llm.user
    assert len(llm.user) <= PROMPT_CONTEXT_MAX_CHARS + 80
    assert "1700000001" not in llm.user
    assert "node_cpu_seconds_total" not in llm.user
    assert '"values"' not in llm.user
    assert "92" in llm.user
    assert "81" in llm.user
    assert "cpu" in llm.user.lower()
    assert "memory" in llm.user.lower() or "mem" in llm.user.lower()


def test_collector_preserves_queries_and_degrades():
    items, limits = collect_evidence_set(
        incident={"number": "INC-1", "title": "Filesystem usage high"},
        asset={"asset_id": "forge-demo-01", "hostname": "forge-demo-01"},
        alert={"alertname": "FilesystemUsageHigh"},
        history=[{"number": "INC-000000", "title": "old"}],
        playrules=[{"name": "high-disk", "playbook": "DISK-FULL"}],
        maintenance=[],
        metric_fetcher=lambda expr: {"value": 94.0, "query": expr},
        log_fetcher=lambda query, start, end: {"lines": [f"ERROR log growth {query}"]},
        window_minutes=30,
        max_log_lines=5,
    )
    assert any(item.type == "METRIC" and item.query == "forgesre_demo_disk_percent" for item in items)
    assert any(item.type == "LOG" and item.query == '{job="forgesre"}' for item in items)
    assert any(item.hash for item in items)
    items_down, limits_down = collect_evidence_set(
        incident={"number": "INC-1", "title": "x"},
        asset={},
        alert={"alertname": "HighCPU"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"error": "down"},
        log_fetcher=lambda query, start, end: {"error": "down"},
    )
    assert "Metrics unavailable." in limits_down
    assert "Logs unavailable." in limits_down


def test_anomaly_hypothesis_and_scoring():
    ctx = RCAContext.from_legacy(
        {
            "incident": {"number": "INC-1042", "title": "Filesystem usage high", "asset": "forge-demo-01"},
            "asset": {"hostname": "forge-demo-01", "asset_id": "forge-demo-01"},
            "alert": {"alertname": "FilesystemUsageHigh"},
            "metrics": {"disk_percent": 94, "cpu_percent": 20},
            "logs": ["ERROR log file growing rapidly"],
            "history": [{"number": "INC-903", "title": "Filesystem usage high"}],
            "playrules": [{"name": "high-disk", "playbook": "DISK-FULL"}],
        }
    )
    ctx.anomalies = detect_anomalies(ctx)
    assert any(item.kind == "threshold_violation" for item in ctx.anomalies)
    facts = facts_from(ctx)
    assert any("94" in fact["text"] for fact in facts)
    hyps = candidate_causes(ctx)
    assert hyps
    assert hyps[0].supporting_evidence
    score = score_confidence(
        anomalies=ctx.anomalies,
        hypotheses=hyps,
        history=ctx.historical_incidents,
        maintenance=[],
        sources_ok=True,
    )
    assert 0.15 <= score <= 0.95


def test_forgerca_cpu_compat():
    result = ForgeRCA(llm=NullLLM()).investigate(
        {
            "incident": {"title": "High CPU", "asset": "forge-demo-01"},
            "asset": {"hostname": "forge-demo-01"},
            "alert": {"alertname": "HighCPU"},
            "metrics": {"cpu_percent": 94, "disk_percent": 20},
            "logs": ["cpu raised"],
            "history": [],
        }
    )
    assert result["disclaimer"] == DISCLAIMER
    assert result["confidence"] >= 70
    assert "forge-demo-01" in result["summary"]
    assert result["provider"] == "builtin-analyst"
    packed = result["result"]
    assert packed["root_cause"]["confidence"] <= 1
    assert packed["facts"]
    assert packed["hypotheses"]
    assert packed["recommended_actions"][0]["executed"] is False

    assert get_engine("forgerca").get_name() == "forgerca"
    assert make_provider(None).get_name() == "none"
    assert validate_recommendation("sudo reboot").startswith("RECOMMENDED ACTION")
    assert extract_json('{"summary": "ok"}') == {"summary": "ok"}
    thinking_only = extract_json("")
    assert thinking_only is None
    fenced = extract_json('prefix\n```json\n{"summary": "fenced"}\n```\n')
    assert fenced == {"summary": "fenced"}
    from app.settings import settings

    assert 60 <= settings.llm_timeout <= 120


def test_forgerca_families_cover_network_storage_windows():
    from rca.catalog import classify_family, hypotheses_for
    from rca.types import RCAContext

    firewall = RCAContext.from_legacy(
        {
            "incident": {"title": "SNMP unreachable", "asset": "fw-01"},
            "asset": {"hostname": "fw-01", "type": "firewall"},
            "alert": {"alertname": "SnmpDeviceUnreachable"},
            "metrics": {"up": 0},
            "logs": [],
            "history": [],
        }
    )
    assert classify_family(firewall) == "firewall"
    ids = {row[0] for row in hypotheses_for("firewall")}
    assert "session-table" in ids
    assert "vpn-down" in ids

    disk = ForgeRCA(llm=NullLLM()).investigate(
        {
            "incident": {"title": "Filesystem usage high", "asset": "db-01"},
            "asset": {"hostname": "db-01", "type": "Linux Server"},
            "alert": {"alertname": "NodeFilesystemUsageHigh"},
            "metrics": {"disk_percent": 94, "cpu_percent": 20},
            "logs": ["ERROR log file growing rapidly"],
            "history": [],
        }
    )
    hyp_ids = [item["id"] for item in disk["result"]["hypotheses"]]
    assert "log-growth" in hyp_ids
    assert "container-overlay" in hyp_ids
    assert disk["summary"].startswith("Disk capacity")

    windows = ForgeRCA(llm=NullLLM()).investigate(
        {
            "incident": {"title": "CPU high", "asset": "win-01"},
            "asset": {"hostname": "win-01", "type": "Windows Server"},
            "alert": {"alertname": "WindowsCPUHigh"},
            "metrics": {"cpu_percent": 96},
            "logs": [],
            "history": [],
        }
    )
    assert "Windows host" in windows["summary"]
    assert any(item["id"] == "win-cpu" for item in windows["result"]["hypotheses"])


def test_playrule_page_has_metric_presets():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    page = client.get("/playrules")
    assert page.status_code == 200
    assert b"playrule-preset" in page.content
    assert b"HighCPU" in page.content
    assert b"cpu_usage" in page.content
    assert b"NetworkInterfaceDown" in page.content
    assert b"default warning" in page.content.lower()
    assert b"assets.alarms" in page.content
    assert b"not a second alerting engine" in page.content
    assert b'href="/playrules">Cancel</a>' in page.content
    books = client.get("/playbooks")
    assert books.status_code == 200
    assert b'href="/playbooks">Cancel</a>' in books.content
    esc = client.get("/escalation")
    assert esc.status_code == 200
    assert b'href="/escalation">Cancel</a>' in esc.content
    assert b"Create escalation policy" in esc.content
    assert b"Default warning" in esc.content
    admin = client.get("/admin")
    assert admin.status_code == 200
    assert b"ForgeRCA" not in admin.content or b"ForgeSRE CLI" in admin.content
    lower = admin.content.lower()
    assert b"forgesre cli" in lower
    assert b"appliance shell" not in lower
    assert b"there is no terminal in this browser" in lower or b"no terminal in this browser" in lower
    assert b">backup<" in lower
    assert b"admin-backup-cli" in admin.content
    assert b"./forgesre shell" in admin.content
    db.close()


def test_audit_and_api_investigation():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.models import AuditLog, Incident
    from app.seed import seed
    from app.services import close_open_incidents, ingest_alertmanager

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    close_open_incidents(db, "FilesystemUsageHigh:forge-demo-01", include_resolved=True)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "FilesystemUsageHigh", "severity": "warning", "asset": "forge-demo-01"},
                "annotations": {"summary": "Filesystem usage high", "description": "94%"},
                "fingerprint": "test-disk",
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    assert created
    incident = created[0]
    from app.jobs import run_pending_jobs

    run_pending_jobs(db)
    db.refresh(incident)
    assert incident.playbook is not None
    assert incident.investigations
    latest = incident.investigations[-1]
    assert latest.result
    assert latest.engine == "forgerca"
    audit_row = db.query(AuditLog).filter_by(action="ai.investigation").order_by(AuditLog.id.desc()).first()
    assert audit_row is not None
    assert "engine" in (audit_row.data or {})

    client = TestClient(app)
    login = client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    assert login.status_code in {302, 303}
    body = client.get(f"/api/v1/incidents/{incident.number}/investigation")
    assert body.status_code == 200
    assert body.json()["result"]["facts"]
    ev = client.get(f"/api/v1/investigations/{latest.id}/evidence")
    assert ev.status_code == 200
    page = client.get(f"/ai/{incident.number}")
    assert page.status_code == 200
    assert b"Facts" in page.content
    db.close()


def _inv_count(db, incident_id: int) -> int:
    from app.models import Investigation

    return db.query(Investigation).filter_by(incident_id=incident_id).count()


def test_automatic_rca_does_not_duplicate_without_force():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.jobs import run_pending_jobs
    from app.main import app
    from app.seed import seed
    from app.services import ingest_alertmanager, run_investigation

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "FilesystemUsageHigh", "severity": "warning", "asset": "idem-fs-01"},
                "annotations": {"summary": "Filesystem usage high", "description": "94%"},
                "fingerprint": "test-disk-once",
            }
        ],
    }
    incident = ingest_alertmanager(db, payload)[0]
    run_pending_jobs(db)
    assert _inv_count(db, incident.id) == 1
    run_investigation(db, incident)
    run_pending_jobs(db)
    assert _inv_count(db, incident.id) == 1
    run_investigation(db, incident, force=True)
    assert _inv_count(db, incident.id) == 2
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    page = client.get(f"/ai/{incident.number}")
    assert page.status_code == 200
    assert b"Facts" in page.content
    db.close()


def test_demo_rca_opens_new_incident_and_runs_builtin_immediately():
    from app.db import Base, SessionLocal, engine
    from app.jobs import run_pending_jobs
    from app.models import Investigation
    from app.seed import seed
    from app.services import run_demo_rca

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    first = run_demo_rca(db)
    assert first is not None
    assert _inv_count(db, first.id) == 1
    latest = (
        db.query(Investigation).filter_by(incident_id=first.id).order_by(Investigation.id.desc()).first()
    )
    assert latest is not None
    assert latest.provider == "builtin-analyst"
    run_pending_jobs(db)
    assert _inv_count(db, first.id) == 1
    run_pending_jobs(db)
    assert _inv_count(db, first.id) == 1
    second = run_demo_rca(db)
    assert second is not None
    assert second.number != first.number
    assert _inv_count(db, second.id) == 1
    run_pending_jobs(db)
    assert _inv_count(db, second.id) == 1
    assert _inv_count(db, first.id) == 1
    db.close()


def test_demo_workflow_redirects_to_open_incident():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    posted = client.post("/demo", follow_redirects=False)
    assert posted.status_code == 303
    location = posted.headers.get("location") or ""
    assert location.startswith("/incidents/INC-")
    page = client.get(location)
    assert page.status_code == 200
    assert "INC-" in page.text
    assert "OPEN" in page.text or "INVESTIGATING" in page.text
    rca = client.post("/demo-rca", follow_redirects=False)
    assert rca.status_code == 303
    location = rca.headers.get("location") or ""
    assert "/ai/INC-" in location
    page = client.get(location)
    assert page.status_code == 200
    assert "ForgeRCA (builtin)" in page.text
    assert "Facts" in page.text
    assert "Limitations" in page.text
    db.close()


def test_investigate_button_opens_builtin_rca_immediately():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.jobs import run_pending_jobs
    from app.main import app
    from app.models import Incident, Investigation, Job
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    incident = Incident(
        number="INC-0099_21.08.2026_11:00",
        title="button queue",
        severity="WARNING",
        status="OPEN",
        fingerprint="button-queue-rca",
    )
    db.add(incident)
    db.commit()
    number = incident.number
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    posted = client.post(f"/incidents/{number}/investigate", follow_redirects=False)
    assert posted.status_code == 303
    assert posted.headers["location"].endswith(f"/ai/{number}")
    db.expire_all()
    assert _inv_count(db, incident.id) == 1
    latest = (
        db.query(Investigation).filter_by(incident_id=incident.id).order_by(Investigation.id.desc()).first()
    )
    assert latest is not None
    assert latest.provider == "builtin-analyst"
    html = client.get(f"/ai/{number}")
    assert html.status_code == 200
    assert "ForgeRCA (builtin)" in html.text
    assert "ForgeRCA" in html.text
    assert "ForgeAI" in html.text
    assert "Summary" in html.text
    assert "Root cause" in html.text
    assert "Recommended actions" in html.text
    assert "Facts" in html.text
    assert "Anomalies" in html.text
    assert "Candidate causes" in html.text
    assert "Limitations" in html.text
    assert "Run now" not in html.text
    assert db.query(Job).filter_by(kind="investigate", object_id=number).first() is None
    again = client.post(f"/incidents/{number}/investigate", follow_redirects=False)
    assert again.status_code == 303
    run_pending_jobs(db)
    db.expire_all()
    assert _inv_count(db, incident.id) == 1
    db.close()


def test_openai_llm_retries_plain_then_thinking_kwargs(monkeypatch):
    import json

    import httpx

    from rca.llm import OpenAICompatibleLLM

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model.gguf"}]})
        body = json.loads(request.content)
        if not body.get("chat_template_kwargs"):
            return httpx.Response(400, json={"error": "need template"})
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '{"summary": "from-llm", "likely_cause": "c", "recommended_action": "a", "limitations": []}'
                    }
                }
            ]
        }
        return httpx.Response(200, json=payload)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    llm = OpenAICompatibleLLM("http://127.0.0.1:8088/v1")
    out = llm.complete_json("sys", "user")
    assert out is not None
    assert out["summary"] == "from-llm"
