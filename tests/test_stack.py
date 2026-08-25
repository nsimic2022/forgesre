from pathlib import Path

from app.api import doctor_payload
from app.stack import (
    doctor_soft_status,
    enrich_components,
    ensure_snmp_exporter,
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
    assert runtime_state({"status": "error", "why": "timed out"}) == ("starting", "warn")
    assert runtime_state({"status": "error", "why": "connection refused"}) == ("down", "crit")
    assert doctor_soft_status("paused") is True
    assert doctor_soft_status("error") is False


def test_ensure_snmp_exporter_skipped_in_dev():
    assert ensure_snmp_exporter() is False


def test_doctor_snmp_paused_when_no_targets(monkeypatch):
    def _http(url, method):
        if "9116" in url:
            return {"status": "error", "why": "connection refused"}
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.api.snmp_target_count", lambda: 0)
    monkeypatch.setattr("app.api.ensure_snmp_exporter", lambda: False)
    payload = doctor_payload(force=True)
    snmp = payload["components"]["snmp"]
    assert snmp["status"] == "paused"
    assert "paused" in (snmp.get("why") or "").lower()
    assert "snmp" not in payload["failed"]
    rows = enrich_components(payload["components"], "lab.local:8080")
    row = next(item for item in rows if item["id"] == "snmp")
    assert row["state"] == "paused"
    assert row["css"] == "warn"


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
    assert "snmp-exporter" in text
    assert "up -d snmp-exporter" in text


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
    prom = next(row for row in rows if row["id"] == "prometheus")
    assert prom["state"] == "down"
    assert prom["gui"].startswith("http://lab.local:")
    assert "/graph" in prom["gui"]
    assert prom["metrics"].endswith("/metrics")
    grafana = next(row for row in rows if row["id"] == "grafana")
    assert grafana["gui"]
