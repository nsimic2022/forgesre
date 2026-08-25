import os

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import approve_candidate, is_snmp_asset, sd_snmp_targets, upsert_candidate
from app.jobs import run_pending_jobs
from app.main import app
from app.metrics import demo_metric_values, set_demo_cpu
from app.models import Asset, Evidence, Job, Playrule, User
from app.security import hash_password
from app.seed import seed
from app.services import close_open_incidents, ingest_alertmanager, next_incident_number, query_prometheus
from app.settings import assert_runtime_secrets
from discovery import classify, snmp_get_sysdescr_packet
from rca.collector import promql_queries_for


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_classify_prefers_snmp_over_ssh_only():
    assert classify([9100], snmp_ok=True) == "Possible Linux server"
    assert classify([22], snmp_ok=True) == "Possible network device"
    assert classify([22], snmp_ok=False) == "Possible Linux server"
    assert classify([161]) == "Possible network device"
    pkt = snmp_get_sysdescr_packet("public")
    assert pkt[0] == 0x30


def test_resolved_alert_does_not_create_incident():
    db = _db()
    payload = {
        "status": "resolved",
        "alerts": [
            {
                "status": "resolved",
                "labels": {"alertname": "NeverSeen", "asset": "forge-demo-01"},
                "annotations": {"summary": "gone"},
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    assert created == []
    db.close()


def test_incident_numbers_use_max_not_count():
    db = _db()
    n1 = next_incident_number(db)
    assert n1.startswith("INC-")
    db.close()


def test_incident_number_has_local_date_and_short_seq():
    from datetime import datetime, timezone

    from app.services import format_incident_number, incident_seq

    assert incident_seq("INC-000012") == 12
    assert incident_seq("INC-0134-16.08.2026-09-13") == 134
    assert incident_seq("INC-0134_16.08.2026_09:13") == 134
    stamp = datetime(2026, 8, 16, 7, 13, tzinfo=timezone.utc)
    number = format_incident_number(134, stamp)
    assert number.startswith("INC-0134_16.08.2026_")
    from app.settings import settings

    if settings.timezone in {"Europe/Belgrade", "Europe/Berlin", "Europe/Zagreb"}:
        assert number == "INC-0134_16.08.2026_09:13"


def test_new_incident_number_increments_legacy_six_digit():
    db = _db()
    from app.models import Incident
    from app.services import incident_seq

    db.add(
        Incident(
            number="INC-000020",
            title="legacy",
            severity="WARNING",
            status="CLOSED",
            fingerprint="legacy-seq-20",
        )
    )
    db.commit()
    nxt = next_incident_number(db)
    assert incident_seq(nxt) == 21
    assert nxt.startswith("INC-0021_")
    db.close()


def test_colon_incident_id_is_routable_and_kept_by_cli():
    from urllib.parse import quote

    from app.cli_ops import _incident_number
    from app.models import Incident

    number = "INC-0134_16.08.2026_09:13"
    assert _incident_number([number]) == number
    assert ":" in quote(number, safe=".-_:")
    db = _db()
    db.add(
        Incident(
            number=number,
            title="colon id",
            severity="WARNING",
            status="OPEN",
            fingerprint="colon-id-route-test",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get(f"/incidents/{number}")
    assert page.status_code == 200
    assert number in page.text
    api = client.get(f"/api/v1/incidents/{number}")
    assert api.status_code == 200
    assert api.json()["number"] == number
    db.close()


def test_demo_gauges_not_applied_to_other_assets():
    db = _db()
    set_demo_cpu(94)
    other = Asset(
        asset_id="app-real-01",
        hostname="app-real-01",
        ip="10.10.10.50",
        type="Linux Server",
        monitoring_profile="linux-standard",
        scrape_address="10.10.10.50:9100",
    )
    db.add(other)
    db.commit()
    db.refresh(other)
    out = query_prometheus(other)
    assert out.get("cpu_percent") != 94
    queries = out.get("queries") or {}
    assert "node_cpu_seconds_total" in str(queries.get("cpu_percent") or "")
    demo = query_prometheus(db.query(Asset).filter_by(asset_id="forge-demo-01").one())
    assert demo.get("cpu_percent") == demo_metric_values()["forgesre_demo_cpu_percent"]
    db.close()


def test_promql_demo_gauges_only_for_demo_asset():
    demo = promql_queries_for({"asset_id": "forge-demo-01"})
    assert demo["cpu_percent"][0] == "forgesre_demo_cpu_percent"
    real = promql_queries_for({"asset_id": "app-real-01", "type": "Linux Server"})
    assert "forgesre_demo_cpu_percent" not in str(real)
    assert "node_cpu_seconds_total" in str(real)
    win = promql_queries_for({"asset_id": "win-real-01", "type": "Windows Server"})
    assert "windows_cpu_time_total" in str(win)
    assert "forgesre_demo_cpu_percent" not in str(win)
    unknown = promql_queries_for({})
    assert "forgesre_demo_cpu_percent" not in str(unknown)


def test_webhook_enqueues_investigation_job():
    db = _db()
    close_open_incidents(db, "HighCPU:forge-demo-01")
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "severity": "warning", "asset": "forge-demo-01"},
                "annotations": {"summary": "High CPU"},
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    assert created
    incident = created[0]
    jobs = db.query(Job).filter_by(kind="investigate", object_id=incident.number).all()
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert (jobs[0].payload or {}).get("use_llm") is False
    db.refresh(incident)
    assert not incident.investigations
    assert db.query(Evidence).filter_by(incident_id=incident.id).count() > 0
    run_pending_jobs(db)
    db.refresh(incident)
    assert incident.investigations
    db.close()


def test_webhook_evidence_collected_once(monkeypatch):
    db = _db()
    close_open_incidents(db, "HighCPU:forge-demo-01")
    from app import services as svc

    counts = {"prom": 0, "expr": 0, "loki": 0}
    real_prom = svc.query_prometheus
    real_expr = svc.query_prometheus_expr
    real_loki = svc.query_loki

    def wrap_prom(*args, **kwargs):
        counts["prom"] += 1
        return real_prom(*args, **kwargs)

    def wrap_expr(*args, **kwargs):
        counts["expr"] += 1
        return real_expr(*args, **kwargs)

    def wrap_loki(*args, **kwargs):
        counts["loki"] += 1
        return real_loki(*args, **kwargs)

    monkeypatch.setattr(svc, "query_prometheus", wrap_prom)
    monkeypatch.setattr(svc, "query_prometheus_expr", wrap_expr)
    monkeypatch.setattr(svc, "query_loki", wrap_loki)

    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "severity": "warning", "asset": "forge-demo-01"},
                "annotations": {"summary": "High CPU"},
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    assert created
    incident = created[0]
    after_ingest = dict(counts)
    assert after_ingest["prom"] == 1
    evidence_n = db.query(Evidence).filter_by(incident_id=incident.id).count()
    assert evidence_n > 0

    ingest_alertmanager(db, payload)
    assert counts == after_ingest
    assert db.query(Evidence).filter_by(incident_id=incident.id).count() == evidence_n

    run_pending_jobs(db)
    assert counts == after_ingest
    assert db.query(Evidence).filter_by(incident_id=incident.id).count() == evidence_n
    db.refresh(incident)
    assert incident.investigations
    db.close()


def test_core_image_requirements_omit_pytest():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runtime = (root / "backend" / "requirements.txt").read_text()
    dev = (root / "requirements-dev.txt").read_text()
    dockerfile = (root / "backend" / "Dockerfile").read_text()
    assert "pytest" not in runtime
    assert "pytest" in dev
    assert "requirements.txt" in dockerfile
    assert "requirements-dev" not in dockerfile


def test_ops_console_on_off_nav_and_ai_anchor():
    db = _db()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    playrules = client.get("/playrules")
    assert playrules.status_code == 200
    assert "On" in playrules.text
    assert "class=\"active\"" in playrules.text
    assert ">True<" not in playrules.text and ">False<" not in playrules.text
    assert 'class="secondary"' in client.get("/").text
    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "class=\"active\"" in admin.text
    discovery = client.get("/discovery")
    assert discovery.status_code == 200
    assert "Enabled:" in discovery.text
    assert ">True<" not in discovery.text and ">False<" not in discovery.text
    close_open_incidents(db, "HighCPU:forge-demo-01")
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "severity": "warning", "asset": "forge-demo-01"},
                    "annotations": {"summary": "High CPU"},
                }
            ],
        },
    )
    page = client.get(f"/incidents/{created[0].number}")
    assert page.status_code == 200
    assert 'id="ai"' in page.text
    db.close()


def test_viewer_cannot_open_playrules_or_journal():
    db = _db()
    if db.query(User).filter_by(email="viewer@forgesre.local").first() is None:
        db.add(User(email="viewer@forgesre.local", name="V", password_hash=hash_password("testpass"), role="viewer"))
        db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "viewer@forgesre.local", "password": "testpass"}, follow_redirects=False)
    assert client.get("/playrules").status_code == 403
    assert client.get("/journal").status_code == 403
    assert client.get("/discovery").status_code == 403
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/system/doctor").status_code == 200
    anon = TestClient(app)
    assert anon.get("/api/v1/system/doctor").status_code == 401
    db.close()


def test_system_health_page_has_doctor_button_and_status_pills():
    db = _db()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    page = client.get("/health-ui")
    assert page.status_code == 200
    assert "Run doctor" in page.text
    assert "Open Grafana" in page.text
    assert "<th>Open</th>" in page.text
    assert 'class="pill ok"' in page.text or 'class="pill warn"' in page.text or 'class="pill crit"' in page.text
    assert "postgres" in page.text
    assert "Core (container)" in page.text
    posted = client.post("/health-ui/refresh", follow_redirects=False)
    assert posted.status_code == 303
    assert posted.headers["location"] == "/health-ui"
    db.close()


def test_playrule_gets_default_escalation_policy():
    db = _db()
    rule = db.query(Playrule).filter_by(name="high-cpu").one()
    assert rule.escalation_policy_id is not None
    node = db.query(Playrule).filter_by(name="node-exporter-down").one()
    assert node.condition.get("alertname") == "NodeExporterDown"
    db.close()


def test_linux_not_in_snmp_sd():
    db = _db()
    linux = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    assert is_snmp_asset(linux) is False
    labels = [(item.get("labels") or {}).get("asset") for item in sd_snmp_targets(db)]
    assert "forge-demo-01" not in labels
    db.close()


def test_approve_ssh_only_linux_has_no_node_exporter_scrape():
    db = _db()
    row = upsert_candidate(db, "10.9.9.9", "Possible Linux server", [22])
    db.commit()
    asset = approve_candidate(db, row, actor="tester")
    db.commit()
    assert asset is not None
    assert asset.scrape_address == ""
    db.close()


def test_runtime_secrets_fail_closed(monkeypatch):
    monkeypatch.delenv("FORGESRE_DEV", raising=False)
    monkeypatch.setenv("SECRET_KEY", "forgesre-dev-secret-change-me")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN", "forgesre-dev-webhook-token")
    with pytest.raises(SystemExit):
        assert_runtime_secrets()
    monkeypatch.setenv("FORGESRE_DEV", "1")
    assert_runtime_secrets()
    monkeypatch.delenv("FORGESRE_DEV")
    monkeypatch.setenv("SECRET_KEY", "real-secret-value")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN", "real-webhook-token")
    assert_runtime_secrets()
    os.environ["FORGESRE_DEV"] = "1"
