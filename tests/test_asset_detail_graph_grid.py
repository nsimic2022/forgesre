"""Asset detail: quarter fact column, equal actions, host chart grid from real series."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.asset_metrics import asset_metric_panel, metric_class_for
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
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


def test_layout_is_a_quarter_fact_column_and_a_chart_grid():
    html = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "asset-detail-col-1-4" in html and "asset-detail-col-2-3" in html
    rule = css.split(".asset-detail-split {", 1)[1].split("}", 1)[0]
    assert "minmax(0, 25%)" in rule and "minmax(0, 1fr)" in rule
    assert "repeat(5, minmax(0, 1fr))" in css
    assert "data-asset-graph-grid" in html
    for key in ("cpu_percent", "memory_percent", "disk_percent", "network", "up"):
        assert f"host_chart('{key}'" in html
    assert "Not scraped." in html
    assert "No samples yet" in html
    assert "grafana" not in html.lower() and "iframe" not in html.lower()
    assert "request rate" not in html.lower() and "5xx" not in html.lower()
    assert "Math.sin" not in js
    assert 'data.collecting === false ? "Not scraped." : "No samples yet."' in js
    assert "app.css?v=v09-N" in base and "app.js?v=v09-N" in base
    asset_js = js.split("(function bindAssetGraphs() {", 1)[1].split("\n})();", 1)[0]
    assert "?incident=" not in asset_js
    incident = (ROOT / "frontend" / "templates" / "incident_detail.html").read_text(encoding="utf-8")
    assert "one hour before" in incident.lower() or "From one hour before" in incident
    assert "data-asset-graph-grid" not in (ROOT / "frontend" / "templates" / "assets.html").read_text(encoding="utf-8")


def test_action_buttons_are_separate_and_only_remove_is_danger():
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    seed(db)
    token = uuid4().hex[:8]
    asset = create_manual_asset(
        db,
        hostname=f"grid-act-{token}",
        asset_id=f"grid-act-{token}",
        ip=f"10.88.{int(token[:2], 16) % 200}.{1 + int(token[2:4], 16) % 200}",
        type="Linux Server",
        actor="tester",
    )
    asset_id = asset.asset_id
    db.close()
    page = _login().get(f"/assets/{asset_id}")
    assert page.status_code == 200
    actions = page.text.split("data-asset-actions", 1)[1].split("</div>", 1)[0]
    assert actions.count("<a ") == 3
    assert actions.count("<form") == 2
    assert actions.count("<button") == 2
    for label in ("Edit", "Clone", "Verify", "Export", "Remove"):
        assert f">{label}<" in actions
    assert actions.count('class="danger"') == 1
    assert 'class="danger">Remove<' in actions
    assert "Detect OS" not in actions
    left = page.text.split("asset-detail-col-1-4", 1)[1].split("asset-detail-side", 1)[0]
    order = [
        'data-asset-section="identity"',
        'data-asset-section="noc"',
        'data-asset-section="support"',
        'data-asset-section="monitoring"',
        'data-asset-section="similar"',
        'data-asset-section="alarms"',
    ]
    spots = [left.index(token) for token in order]
    assert spots == sorted(spots)
    assert left.index('data-asset-section="alarms"') < page.text.index("data-asset-actions")
    assert page.text.count('data-asset-section="similar"') == 1
    assert "Similar incident history" in page.text


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
    assert abs((panel["window"]["end"] - panel["window"]["start"]) - 3600) < 2
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


def test_export_button_downloads_this_asset():
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    seed(db)
    token = uuid4().hex[:8]
    host = f"grid-exp-{token}"
    asset = create_manual_asset(
        db,
        hostname=host,
        asset_id=host,
        ip=f"10.89.{int(token[:2], 16) % 200}.{1 + int(token[2:4], 16) % 200}",
        type="Linux Server",
        actor="tester",
    )
    asset_id = asset.asset_id
    db.close()
    client = _login()
    exported = client.post("/assets/export", data={"selected": asset_id})
    assert exported.status_code == 200
    assert asset_id in exported.text
    assert host in exported.text
