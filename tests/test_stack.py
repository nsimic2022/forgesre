from app.stack import enrich_components, rewrite_host, runtime_state


def test_rewrite_host_swaps_loopback_for_request_host():
    assert rewrite_host("http://127.0.0.1:9090/graph", "vm.example") == "http://vm.example:9090/graph"
    assert rewrite_host("http://localhost:3000", "10.1.2.3") == "http://10.1.2.3:3000"
    assert rewrite_host("http://grafana.corp:3000", "vm.example") == "http://grafana.corp:3000"


def test_runtime_state_maps_green_yellow_red():
    assert runtime_state({"status": "ok"}) == ("running", "ok")
    assert runtime_state({"status": "disabled"}) == ("paused", "warn")
    assert runtime_state({"status": "error", "why": "timed out"}) == ("starting", "warn")
    assert runtime_state({"status": "error", "why": "connection refused"}) == ("down", "crit")


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
