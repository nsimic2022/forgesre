"""Review fixes: doctor probes, Loki skip, LLM timeout, update --offline."""

from pathlib import Path

from rca.collector import (
    DEMO_LOGS_LIMITATION,
    HOST_LOGS_LIMITATION,
    collect_evidence_set,
    loki_query_for,
)

from app.api import doctor_payload
from app.jobs import job_is_llm, next_pending_job
from app.models import Job

ROOT = Path(__file__).resolve().parents[1]


def test_loki_query_skips_real_inventory_hosts():
    assert loki_query_for({"asset_id": "forge-demo-01"}) == '{job="forgesre"}'
    assert loki_query_for({"asset_id": "win10-gp"}) is None
    assert loki_query_for({"asset_id": "db-01"}) is None


def test_collector_does_not_present_empty_loki_as_host_logs():
    called = {"n": 0}

    def log_fetcher(query, start, end):
        called["n"] += 1
        return {"lines": [f"should-not-count {query}"]}

    items, limits = collect_evidence_set(
        incident={"number": "INC-1", "title": "High CPU"},
        asset={"asset_id": "win10-gp", "hostname": "DESKTOP-X", "type": "Windows Server"},
        alert={"alertname": "WindowsCPUHigh"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"value": 10.0, "query": expr},
        log_fetcher=log_fetcher,
        window_minutes=30,
        max_log_lines=5,
    )
    assert called["n"] == 0
    assert HOST_LOGS_LIMITATION in limits
    assert not any(item.type == "LOG" for item in items)


def test_collector_demo_loki_is_appliance_logs_labeled_demo():
    items, limits = collect_evidence_set(
        incident={"number": "INC-1", "title": "High CPU"},
        asset={"asset_id": "forge-demo-01", "hostname": "forge-demo-01"},
        alert={"alertname": "HighCPU"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"value": 94.0, "query": expr},
        log_fetcher=lambda query, start, end: {"lines": [f"core log {query}"]},
        window_minutes=30,
        max_log_lines=5,
    )
    assert DEMO_LOGS_LIMITATION in limits
    logs = [item for item in items if item.type == "LOG"]
    assert logs
    assert logs[0].query == '{job="forgesre"}'
    assert logs[0].metadata.get("label") == "DEMO"
    assert logs[0].metadata.get("scope") == "appliance-demo"


def test_doctor_core_probes_health_url(monkeypatch):
    seen: list[str] = []

    def _http(url, method):
        seen.append(url)
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.journal.list_entries", lambda *a, **k: [])
    monkeypatch.setattr("app.inventory.discovery_loop_age_seconds", lambda: 1.0)
    payload = doctor_payload(force=True)
    assert any("/api/v1/health" in url for url in seen)
    core = payload["components"]["core"]
    assert core["status"] == "ok"
    assert core["label"] == "Core (container)"
    assert "core" in payload["components"]


def test_doctor_discovery_is_not_a_checkbox(monkeypatch):
    monkeypatch.setattr("app.api._http", lambda url, method: {"status": "ok"})
    monkeypatch.setattr("app.journal.list_entries", lambda *a, **k: [])
    monkeypatch.setattr("app.inventory.discovery_loop_age_seconds", lambda: None)
    payload = doctor_payload(force=True)
    disc = payload["components"]["discovery"]
    assert disc["status"] == "warn"
    assert "loop" in (disc.get("why") or "").lower() or "reported" in (disc.get("why") or "").lower()
    monkeypatch.setattr("app.inventory.discovery_loop_age_seconds", lambda: 4.0)
    payload = doctor_payload(force=True)
    assert payload["components"]["discovery"]["status"] == "ok"
    assert "alive" in (payload["components"]["discovery"].get("why") or "").lower()


def test_jobs_loop_runs_reports_before_pending_jobs():
    text = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    loop = text[text.index("def _jobs_loop") : text.index("def _escalation_loop")]
    assert loop.index("process_scheduled_reports(db)") < loop.index("run_pending_jobs(db)")


def test_next_pending_job_prefers_builtin_rca_over_llm():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(Job).filter_by(status="pending").delete(synchronize_session=False)
    db.commit()
    llm = Job(kind="investigate", status="pending", object_id="INC-LLM", payload={"use_llm": True})
    builtin = Job(kind="investigate", status="pending", object_id="INC-RCA", payload={"use_llm": False})
    db.add(llm)
    db.commit()
    db.add(builtin)
    db.commit()
    picked = next_pending_job(db)
    assert picked is not None
    assert picked.object_id == "INC-RCA"
    assert job_is_llm(picked) is False
    db.query(Job).filter(Job.object_id.in_(["INC-LLM", "INC-RCA"])).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_update_script_skips_blind_pull_and_build():
    text = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "--offline" in text
    assert "skipping --build" in text
    assert "skipping compose pull" in text
    assert "backend/Dockerfile" in text
    assert ".core-image.stamp" in text
    assert "first boot can take several minutes" in text.lower() or "migrations" in text.lower()
    assert "yellow" in text.lower()
    help_txt = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert "./forgesre update --offline" in help_txt
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "--offline" in cli


def test_example_yaml_ai_off_and_short_timeout():
    from app.settings import settings

    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    assert "enabled: false" in example
    assert "timeout_seconds: 90" in example
    assert "mailpit" not in example.lower()
    assert 60 <= settings.llm_timeout <= 120
    docs = " ".join(
        (ROOT / "docs" / name).read_text(encoding="utf-8").lower()
        for name in ("cli.md", "install-config.md", "operator-handbook.md", "continuation.md", "v0.7.md")
    )
    assert "mailpit" not in docs
