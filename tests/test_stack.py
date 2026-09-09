from pathlib import Path

from app.api import doctor_payload
from app.db import Base, SessionLocal, engine
from app.journal import list_entries
from app.seed import seed
from app.stack import (
    alarm_path_failure_lines,
    component_label,
    doctor_soft_status,
    enrich_components,
    ensure_snmp_exporter,
    journal_doctor_alarm_path,
    rewrite_host,
    runtime_state,
)

ROOT = Path(__file__).resolve().parents[1]


def test_rewrite_host_swaps_loopback_for_request_host():
    assert rewrite_host("http://127.0.0.1:9090/graph", "vm.example") == "http://vm.example:9090/graph"
    assert rewrite_host("http://localhost:3000", "10.1.2.3") == "http://10.1.2.3:3000"
    assert rewrite_host("http://grafana.corp:3000", "vm.example") == "http://grafana.corp:3000"


def test_runtime_state_maps_green_yellow_red():
    assert runtime_state({"status": "ok"}) == ("running", "ok")
    assert runtime_state({"status": "disabled"}) == ("paused", "warn")
    assert runtime_state({"status": "paused"}) == ("paused", "warn")
    assert runtime_state({"status": "warn"}) == ("warn", "warn")
    assert runtime_state({"status": "warning"}) == ("warn", "warn")
    assert runtime_state({"status": "starting"}) == ("starting", "warn")
    assert runtime_state({"status": "error", "why": "timed out"}) == ("starting", "warn")
    assert runtime_state({"status": "error", "why": "connection refused"}) == ("down", "crit")
    assert doctor_soft_status("paused") is True
    assert doctor_soft_status("warn") is True
    assert doctor_soft_status("starting") is True
    assert doctor_soft_status("error") is False


def test_ensure_snmp_exporter_skipped_in_dev():
    assert ensure_snmp_exporter() is False


def test_doctor_snmp_paused_when_no_targets(monkeypatch):
    def _http(url, method):
        if "9116" in url:
            return {"status": "ok"}
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.api.snmp_target_count", lambda: 0)
    monkeypatch.setattr("app.api.ensure_snmp_exporter", lambda: False)
    payload = doctor_payload(force=True)
    snmp = payload["components"]["snmp"]
    assert snmp["status"] == "paused"
    assert "paused (no SNMP targets)" in (snmp.get("why") or "")
    assert "not down" in (snmp.get("why") or "").lower()
    assert "snmp" not in payload["failed"]
    rows = enrich_components(payload["components"], "lab.local:8080")
    row = next(item for item in rows if item["id"] == "snmp")
    assert row["state"] == "paused (no SNMP targets)"
    assert row["css"] == "warn"
    assert row["label"] == "SNMP exporter"


def test_doctor_snmp_stays_paused_when_exporter_up_but_no_targets(monkeypatch):
    monkeypatch.setattr("app.api._http", lambda url, method: {"status": "ok"})
    monkeypatch.setattr("app.api.snmp_target_count", lambda: 0)
    monkeypatch.setattr("app.api.ensure_snmp_exporter", lambda: True)
    payload = doctor_payload(force=True)
    assert payload["components"]["snmp"]["status"] == "paused"
    assert "snmp" not in payload["failed"]


def test_doctor_snmp_down_when_network_targets_and_exporter_dark(monkeypatch):
    def _http(url, method):
        if "9116" in url:
            return {"status": "error", "why": "connection refused"}
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.api.snmp_target_count", lambda: 1)
    monkeypatch.setattr("app.api.ensure_snmp_exporter", lambda: False)
    payload = doctor_payload(force=True)
    assert payload["components"]["snmp"]["status"] == "error"
    assert "snmp" in payload["failed"]
    assert payload["overall"] == "DEGRADED"


def test_doctor_snmp_running_after_compose_start(monkeypatch):
    hits = {"n": 0}

    def _http(url, method):
        if "9116" in url:
            hits["n"] += 1
            if hits["n"] >= 2:
                return {"status": "ok"}
            return {"status": "error", "why": "connection refused"}
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.api.snmp_target_count", lambda: 1)
    monkeypatch.setattr("app.api.ensure_snmp_exporter", lambda: True)
    monkeypatch.setattr("app.api.time.sleep", lambda _s: None)
    payload = doctor_payload(force=True)
    assert payload["components"]["snmp"]["status"] == "ok"
    assert "snmp" not in payload["failed"]
    rows = enrich_components(payload["components"], "lab.local")
    row = next(item for item in rows if item["id"] == "snmp")
    assert row["state"] == "running"


def test_doctor_script_treats_paused_as_ok_and_starts_compose():
    text = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert '"paused"' in text
    assert '"starting"' in text
    assert "snmp-exporter" in text
    assert "up -d snmp-exporter" in text


def test_component_label_core_is_container_not_api(monkeypatch):
    assert component_label("core") == "Core (container)"
    assert component_label("postgres") == "postgres"
    assert component_label("prometheus") == "Prometheus"
    assert component_label("alertmanager") == "Alertmanager"
    assert component_label("grafana") == "Grafana"
    assert component_label("snmp") == "SNMP exporter"
    assert component_label("netbox") == "NetBox"
    assert "Stack" not in component_label("prometheus")
    monkeypatch.setattr("app.api._http", lambda url, method: {"status": "ok"})
    payload = doctor_payload(force=True)
    core = payload["components"]["core"]
    assert "core" in payload["components"]
    assert core["status"] == "ok"
    assert core["label"] == "Core (container)"


def test_doctor_script_prints_core_api_for_health_curl():
    text = (ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert 'ok "Core API"' in text
    assert 'bad "Core API"' in text
    assert 'ok "Core"' not in text
    assert 'item.get("label")' in text
    assert "/api/v1/health" in text
    health_start = text.index('bad "Core API"')
    doctor_fetch = text.index("Could not fetch /api/v1/system/doctor")
    assert health_start < doctor_fetch
    health_fail = text[health_start:doctor_fetch]
    assert "exit 1" in health_fail
    assert "Could not reach GET /api/v1/health" in health_fail
    assert "webhook token is missing" not in health_fail
    assert "docker compose logs core --tail=80" in health_fail
    token_fail = text[doctor_fetch:]
    assert "webhook token is missing or wrong" in token_fail
    assert "secrets-check" in token_fail
    update = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "Waiting for Core" in update
    assert "/api/v1/health" in update
    assert "docker compose logs core --tail=80" in update


def test_enrich_components_keeps_stack_order_and_open_links():
    rows = enrich_components(
        {
            "core": {"status": "ok"},
            "postgres": {"status": "ok"},
            "prometheus": {"status": "error", "why": "connection refused"},
        },
        "lab.local:8080",
    )
    ids = [row["id"] for row in rows]
    assert ids[:4] == ["core", "postgres", "prometheus", "alertmanager"]
    assert "grafana" in ids
    assert "alloy" in ids
    assert "discovery" in ids
    core = next(row for row in rows if row["id"] == "core")
    assert core["id"] == "core"
    assert core["label"] == "Core (container)"
    assert core["gui"] == "/"
    assert core["metrics"] == "/metrics"
    prom = next(row for row in rows if row["id"] == "prometheus")
    assert prom["state"] == "down"
    assert prom["gui"].startswith("http://lab.local:")
    assert prom["gui"].endswith("/targets?search=")
    assert prom["gui_label"] == "Targets"
    assert prom["metrics"].endswith("/alerts")
    assert prom["metrics_label"] == "Alerts"
    assert prom["extra"].endswith("/graph")
    assert prom["extra_label"] == "Graph"
    assert not prom["metrics"].endswith("/metrics")
    grafana = next(row for row in rows if row["id"] == "grafana")
    assert grafana["gui"]
    assert grafana["label"] == "Grafana"
    prom_labeled = next(row for row in rows if row["id"] == "prometheus")
    assert prom_labeled["label"] == "Prometheus"
    assert "Stack" not in prom_labeled["label"]


def test_doctor_grafana_down_is_warn_not_prometheus_fail(monkeypatch):
    def _http(url, method):
        if ":3000" in url or "3000" in url:
            return {"status": "error", "why": f"{url}: connection refused", "test": f"curl -fsS {url}"}
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.api._probe_sql", lambda: {"status": "ok"})
    monkeypatch.setattr("app.api._discovery_check", lambda: {"status": "ok", "why": "alive"})
    monkeypatch.setattr("app.api._snmp_check", lambda: {"status": "paused", "why": "paused"})
    monkeypatch.setattr("app.api._netbox_check", lambda: {"status": "disabled"})
    payload = doctor_payload(force=True)
    grafana = payload["components"]["grafana"]
    assert grafana["status"] == "warn"
    assert "grafana" not in payload["failed"]
    assert "prometheus" not in payload["failed"]
    assert payload["components"]["prometheus"]["status"] == "ok"
    assert payload["overall"] == "HEALTHY"
    assert "graphs only" in (grafana.get("why") or "").lower()
    rows = enrich_components(payload["components"], "lab.local")
    row = next(item for item in rows if item["id"] == "grafana")
    assert row["css"] == "warn"
    assert row["state"] == "warn"


def test_healthy_prom_check_does_not_journal_error():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    before = [row.id for row in list_entries(db, status="error")]
    components = {
        "prometheus": {"status": "ok", "why": ""},
        "alertmanager": {"status": "ok", "why": ""},
        "grafana": {
            "status": "warn",
            "why": "Grafana is graphs only. Yellow, not a Prometheus outage. connection refused",
        },
    }
    assert alarm_path_failure_lines(components) == []
    journal_doctor_alarm_path(db, components)
    after = list_entries(db, status="error")
    new = [row for row in after if row.id not in before]
    assert not any(row.action == "doctor" for row in new)
    db.close()


def test_unhealthy_prom_journals_failing_hop_not_stack():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    components = {
        "prometheus": {
            "status": "error",
            "why": "http://127.0.0.1:9090/-/ready: connection refused",
            "test": "curl -fsS http://127.0.0.1:9090/-/ready",
        },
        "alertmanager": {"status": "ok", "why": ""},
        "grafana": {"status": "warn", "why": "graphs only"},
    }
    lines = alarm_path_failure_lines(components)
    assert lines
    assert any(":9090" in line for line in lines)
    assert all("Prometheus Stack" not in line for line in lines)
    assert all("grafana" not in line.lower() for line in lines)
    journal_doctor_alarm_path(db, components)
    rows = [row for row in list_entries(db, module="core") if row.action == "doctor"]
    assert rows
    latest = rows[0]
    assert latest.status == "error"
    assert ":9090" in (latest.summary or "")
    assert "Prometheus Stack" not in (latest.summary or "")
    assert "Grafana" not in (latest.summary or "")
    journal_doctor_alarm_path(db, components)
    again = [row for row in list_entries(db, module="core") if row.action == "doctor"]
    assert len(again) == len(rows)
    db.close()


def test_unhealthy_alertmanager_journals_am_hop():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    components = {
        "prometheus": {"status": "ok"},
        "alertmanager": {
            "status": "error",
            "why": "http://127.0.0.1:9093/-/ready: connection refused",
            "test": "curl -fsS http://127.0.0.1:9093/-/ready",
        },
        "grafana": {"status": "ok"},
    }
    journal_doctor_alarm_path(db, components)
    latest = next(row for row in list_entries(db, module="core") if row.action == "doctor")
    assert latest.status == "error"
    assert ":9093" in (latest.summary or "")
    assert "Alertmanager" in (latest.summary or "")
    assert "Prometheus Stack" not in (latest.summary or "")
    db.close()


def test_doctor_recovery_journals_ok_after_prom_error():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    down = {
        "prometheus": {
            "status": "error",
            "why": "http://127.0.0.1:9090/-/ready: connection refused",
            "test": "curl -fsS http://127.0.0.1:9090/-/ready",
        },
        "alertmanager": {"status": "ok"},
    }
    journal_doctor_alarm_path(db, down)
    journal_doctor_alarm_path(db, {"prometheus": {"status": "ok"}, "alertmanager": {"status": "ok"}})
    rows = [row for row in list_entries(db, module="core") if row.action == "doctor"]
    assert rows[0].status == "ok"
    assert "Prometheus Stack" not in (rows[0].summary or "")
    db.close()


def test_http_why_includes_probe_url():
    from app.api import _http

    result = _http("http://127.0.0.1:9/-/ready", "GET")
    assert result["status"] == "error"
    assert "127.0.0.1:9" in (result.get("why") or "")
    assert ":9" in (result.get("why") or "")
