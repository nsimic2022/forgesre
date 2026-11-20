"""Asset detail: quarter fact column, equal actions, host chart grid from real series."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.asset_metrics import asset_metric_panel, metric_class_for
from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed
from rca.collector import promql_queries_for

ROOT = Path(__file__).resolve().parents[1]


def _login() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    return client


def test_linux_network_is_two_real_lines_and_snmp_skips_cpu():
    def query(expr: str) -> dict:
        if "up{" in expr:
            return {"value": 1.0, "query": expr}
        if "node_network_receive" in expr:
            return {"value": 2500.0, "query": expr}
        if "node_network_transmit" in expr:
            return {"value": 1_500_000.0, "query": expr}
        if "node_cpu" in expr:
            return {"value": 10.0, "query": expr}
        return {"value": None, "query": expr}

    def ranged(expr: str) -> dict:
        if "node_network_receive" in expr:
            return {"values": [1000.0, 2000.0, 3000.0], "times": [10, 20, 30], "query": expr}
        if "node_network_transmit" in expr:
            return {"values": [1_000_000.0, 1_500_000.0, 2_000_000.0], "times": [10, 20, 30], "query": expr}
        if "up{" in expr:
            return {"values": [1.0, 1.0, 1.0], "times": [10, 20, 30], "query": expr}
        if "node_cpu" in expr:
            return {"values": [10.0, 12.0, 11.0], "times": [10, 20, 30], "query": expr}
        return {"values": [], "query": expr}

    panel = asset_metric_panel(
        {"asset_id": "grid-linux", "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=query,
        range_fn=ranged,
    )
    assert abs((panel["window"]["end"] - panel["window"]["start"]) - 24 * 3600) < 2
    assert panel["window"]["marker"] is None
    net = next(tile for tile in panel["tiles"] if tile["key"] == "network")
    assert net["unit"] == "MB/s"
    assert [line["name"] for line in net["lines"]] == ["RX", "TX"]
    assert net["lines"][0]["className"] == "rx"
    assert net["lines"][1]["series"] == [1.0, 1.5, 2.0]
    assert "RX" in net["display"] and "TX" in net["display"]
    assert "sin" not in net["query"].lower()

    queries = promql_queries_for({"asset_id": "grid-win", "type": "Windows Server"})
    assert "windows_net_bytes_received_total" in queries["net_rx"][0]
    assert "windows_net_bytes_sent_total" in queries["net_tx"][0]
    assert "ifHCInOctets" not in str(queries)

    assert metric_class_for({"asset_id": "grid-prn", "type": "Printer"}) == "network"
    snmp = promql_queries_for({"asset_id": "grid-prn", "type": "Printer", "ip": "10.1.1.8"})
    assert "ifHCInOctets" in snmp["net_rx"][0] and "ifHCOutOctets" in snmp["net_tx"][0]
    assert "node_cpu_seconds_total" not in str(snmp)
    assert "cpu_percent" not in snmp
    linux_alert = promql_queries_for(
        {"asset_id": "grid-linux", "type": "Linux Server"},
        {"alertname": "StorageVolumeUsageHigh"},
    )
    assert "node_cpu_seconds_total" in linux_alert["cpu_percent"][0]
    assert "ifHCInOctets" not in linux_alert["cpu_percent"][0]


