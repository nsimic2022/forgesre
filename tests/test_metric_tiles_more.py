"""Host metric card, phase B: warning and critical lines from alert severities, UPS and
switch tiles by device type, the period choice and the Grafana link. No live Prometheus."""

from app.asset_metrics import alert_levels, asset_metric_panel, extra_tiles
from tests.test_asset_metrics import _login, _no_spark, _query


def _ranged(mapping):
    def _inner(expr: str) -> dict:
        for needle, values in mapping.items():
            if needle in expr:
                return {"values": values, "times": [1000.0 + 60 * i for i in range(len(values))]}
        return {"values": []}

    return _inner


def test_warning_and_critical_lines_come_from_rule_severity(tmp_path, monkeypatch):
    (tmp_path / "alerts.yml").write_text(
        "groups:\n  - name: t\n    rules:\n"
        "      - alert: NodeCPUHigh\n        expr: cpu > 80\n        labels: {severity: warning}\n"
        "      - alert: NodeCPUHigh\n        expr: cpu > 95\n        labels: {severity: critical}\n"
        "      - alert: NodeMemoryHigh\n        expr: mem > 90\n        labels: {severity: critical}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FORGESRE_MONITORING_DIR", str(tmp_path))
    assert alert_levels()["linux"]["cpu_percent"] == {"warning": 80.0, "critical": 95.0}
    panel = asset_metric_panel(
        {"asset_id": "fm-lin", "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=_query({"node_cpu_seconds_total": 85.0, "node_memory_MemAvailable": 40.0, "node_filesystem_avail": 50.0, "up{": 1.0}),
        range_fn=_no_spark,
    )
    tiles = {t["key"]: t for t in panel["tiles"]}
    assert (tiles["cpu_percent"]["warn"], tiles["cpu_percent"]["crit"], tiles["cpu_percent"]["threshold"]) == (80.0, 95.0, 80.0)
    assert "warn" not in tiles["memory_percent"]  # one level only: one line, as before
    # an asset with its own CPU threshold keeps its single line
    own = asset_metric_panel(
        {"asset_id": "fm-lin", "type": "Linux Server", "monitoring_profile": "linux-standard", "alarms": {"cpu_percent": {"threshold": 70}}},
        query_fn=_query({"node_cpu_seconds_total": 85.0, "up{": 1.0}), range_fn=_no_spark,
    )
    assert "warn" not in {t["key"]: t for t in own["tiles"]}["cpu_percent"]


def test_ups_tiles_and_switch_error_tiles():
    ups = extra_tiles(
        {"asset_id": "fm-ups", "type": "UPS"}, "", False,
        _query({"upsOutputSource": 0.0, "upsEstimatedChargeRemaining": 35.0, "upsOutputPercentLoad": 42.0,
                "upsEstimatedMinutesRemaining": 8.0, "upsInputVoltage": "error", "upsBatteryTemperature": 29.0}),
        _ranged({"upsEstimatedChargeRemaining": [100.0, 80.0, 35.0]}),
    )
    by = {t["key"]: t for t in ups}
    assert list(by) == ["ups_mains", "ups_charge", "ups_load", "ups_minutes", "ups_temp"]  # no voltage: query failed
    assert by["ups_mains"]["display"] == "on battery" and by["ups_mains"]["kind"] == "up"
    assert by["ups_charge"]["tone"] == "warn" and by["ups_charge"]["series"] == [100.0, 80.0, 35.0]
    assert by["ups_minutes"]["tone"] == "crit" and by["ups_minutes"]["unit"] == "min" and by["ups_minutes"]["display"] == "8 min"
    assert by["ups_load"]["threshold"] == 80.0 and 'asset="fm-ups"' in by["ups_load"]["query"]
    sw = extra_tiles({"asset_id": "fm-sw", "type": "Switch"}, 'asset="fm-sw"', True,
                     _query({"if(In|Out)": 2.5, "ifOperStatus": 3.0}), _no_spark)
    by = {t["key"]: t for t in sw}
    assert by["if_errors"]["display"] == "2.5 /s" and by["if_errors"]["tone"] == "high"
    assert by["ports_down"]["display"] == "3" and 'job="forgesre-snmp",asset="fm-sw"' in by["ports_down"]["query"]
    assert extra_tiles({"asset_id": "fm-lin", "type": "Linux Server"}, "", False, _query({}), _no_spark) == []


def test_metrics_api_period_and_grafana_link():
    client = _login("viewer-metrics@example.com")
    assets = client.get("/api/v1/inventory").json()["rows"]
    asset_id = assets[0]["asset_id"]
    assert client.get(f"/api/v1/assets/{asset_id}/metrics?hours=5").status_code == 400
    panel = client.get(f"/api/v1/assets/{asset_id}/metrics?hours=6").json()
    assert round(panel["window"]["end"] - panel["window"]["start"]) == 6 * 3600
    assert "grafana" in panel


def test_cli_metrics_text():
    from app.cli_ops import format_metrics

    text = format_metrics("db-01", {"tiles": [
        {"key": "cpu_percent", "name": "CPU", "display": "88%", "tone": "crit", "threshold": 80, "warn": 80, "crit": 95, "series": [40.0, 60.0, 88.0]},
        {"key": "ups_minutes", "name": "Runtime left", "display": "30 min", "tone": "ok", "threshold": None, "series": []},
    ]}, "6h")
    assert "last 6h" in text
    assert "! CPU" in text and "40.0" in text and "62.7" in text and "88.0" in text and "warning 80 · 95" in text
    assert "Runtime left" in text and "—" in text
    assert "No metrics" in format_metrics("x", {"tiles": []}, "1h")
