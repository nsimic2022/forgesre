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
    assert "specialists" in llm.user
    assert "sentences" in llm.system
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
        asset={"asset_id": "db-01", "hostname": "db-01", "ip": "10.1.1.8"},
        alert={"alertname": "HighCPU"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"error": "down"},
        log_fetcher=lambda query, start, end: {"error": "down"},
    )
    assert "Metrics unavailable." in limits_down
    assert "Logs unavailable." in limits_down
    assert all('job="forgesre"' not in (item.query or "") for item in items_down)


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
    assert any("No process list was collected" in fact["text"] for fact in facts)
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
    assert 10 <= result["confidence"] <= 95
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


def _inv_count(db, incident_id: int) -> int:
    from app.models import Investigation

    return db.query(Investigation).filter_by(incident_id=incident_id).count()


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


def test_openai_llm_sends_thinking_switch_first(monkeypatch):
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

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("rca.llm.httpx.AsyncClient", fake_client)
    llm = OpenAICompatibleLLM("http://127.0.0.1:8088/v1")
    out = llm.complete_json("sys", "user")
    assert out is not None
    assert out["summary"] == "from-llm"
