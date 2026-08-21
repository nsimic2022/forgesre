from rca.analysis import candidate_causes, detect_anomalies, facts_from, score_confidence
from rca.collector import collect_evidence_set
from rca.engines import DISCLAIMER, ForgeRCA, get_engine
from rca.llm import NullLLM, extract_json, make_provider, validate_recommendation
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

    assert settings.llm_timeout >= 600


def test_audit_and_api_investigation():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.models import AuditLog, Incident
    from app.seed import seed
    from app.services import ingest_alertmanager

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
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


def test_demo_rca_opens_new_incident_and_queues_one_investigation():
    from app.db import Base, SessionLocal, engine
    from app.jobs import run_pending_jobs
    from app.seed import seed
    from app.services import run_demo_rca

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    first = run_demo_rca(db)
    assert first is not None
    assert _inv_count(db, first.id) == 0
    run_pending_jobs(db)
    assert _inv_count(db, first.id) == 1
    run_pending_jobs(db)
    assert _inv_count(db, first.id) == 1
    second = run_demo_rca(db)
    assert second is not None
    assert second.number != first.number
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
    assert "/ai/INC-" in (rca.headers.get("location") or "")
    db.close()


def test_investigate_button_queues_job_instead_of_blocking():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.jobs import run_pending_jobs
    from app.main import app
    from app.models import Incident, Job
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
    assert posted.status_code == 302
    assert posted.headers["location"].endswith(f"/ai/{number}")
    assert _inv_count(db, incident.id) == 0
    job = db.query(Job).filter_by(kind="investigate", object_id=number).first()
    assert job is not None
    assert job.status in {"pending", "running", "done"}
    run_pending_jobs(db)
    assert _inv_count(db, incident.id) == 1
    again = client.post(f"/incidents/{number}/investigate", follow_redirects=False)
    assert again.status_code == 302
    run_pending_jobs(db)
    assert _inv_count(db, incident.id) == 2
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
