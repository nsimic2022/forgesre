"""Class-based metric tiles on asset detail. No live Prometheus required."""

from fastapi.testclient import TestClient

from app.asset_metrics import asset_metric_panel, bundled_thresholds, metric_class_for, safe_asset_metric_panel
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.metrics import reset_demo_gauges, set_demo_cpu, set_demo_disk
from app.models import User
from app.security import hash_password
from app.seed import DEMO_ASSET, DEMO_SW_ASSET, DEMO_WIN_ASSET, seed
from rca.collector import promql_queries_for


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(email: str, password: str = "testpass") -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    if db.query(User).filter_by(email=email).first() is None:
        role = "viewer" if email.startswith("viewer") else "analyst"
        db.add(
            User(
                email=email,
                name="Test",
                password_hash=hash_password(password),
                role=role,
            )
        )
        db.commit()
    db.close()
    client = TestClient(app)
    login = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert login.status_code in {302, 303}
    return client


def _query(mapping):
    def _inner(expr: str) -> dict:
        for needle, value in mapping.items():
            if needle in expr:
                if value == "error":
                    return {"error": "connection refused", "query": expr}
                return {"value": value, "query": expr}
        return {"value": None, "query": expr}

    return _inner


def _no_spark(_expr: str) -> dict:
    return {"values": []}


def test_bundled_thresholds_match_alerts_yml_not_guessed_80():
    values = bundled_thresholds()
    assert values["demo"]["cpu_percent"] == 80
    assert values["demo"]["disk_percent"] == 80
    assert values["linux"]["cpu_percent"] == 95
    assert values["linux"]["disk_percent"] == 90
    assert values["linux"]["memory_percent"] == 90
    assert values["windows"]["cpu_percent"] == 90
    assert values["windows"]["disk_percent"] == 90
    alerts = (open("monitoring/alerts.yml", encoding="utf-8")).read()
    assert "NodeCPUHigh" in alerts
    assert "> 95" in alerts
    assert "WindowsCPUHigh" in alerts
    assert "> 90" in alerts


def test_metric_class_linux_windows_network_demo_unknown():
    assert metric_class_for({"asset_id": DEMO_ASSET, "type": "Linux Server"}) == "demo"
    assert metric_class_for({"asset_id": "app-01", "type": "Linux Server"}) == "linux"
    assert metric_class_for({"asset_id": "win-01", "type": "Windows Server"}) == "windows"
    assert metric_class_for({"asset_id": "sw-01", "type": "Network Switch"}) == "network"
    assert metric_class_for({"asset_id": "box-01", "type": "Auto (detect exporter)"}) == "unknown"


def test_linux_tiles_cpu_below_95_is_green():
    panel = asset_metric_panel(
        {"asset_id": "app-lab-01", "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=_query(
            {
                "node_cpu_seconds_total": 90.0,
                "node_memory_MemAvailable": 40.0,
                "node_filesystem_avail": 50.0,
                'up{': 1.0,
            }
        ),
        range_fn=_no_spark,
    )
    by_key = {tile["key"]: tile for tile in panel["tiles"]}
    assert list(by_key) == ["up", "cpu_percent", "memory_percent", "disk_percent"]
    assert by_key["cpu_percent"]["threshold"] == 95
    assert by_key["cpu_percent"]["tone"] == "ok"
    assert by_key["cpu_percent"]["display"] == "90%"
    assert by_key["cpu_percent"]["value"] == 90.0
    assert by_key["up"]["tone"] == "ok"
    assert by_key["up"]["display"] == "up"
    assert panel["collecting"] is True
    assert "up=1" in panel["collecting_line"]
    blob = str(promql_queries_for({"asset_id": "app-lab-01", "type": "Linux Server"}))
    assert "node_cpu_seconds_total" in blob


def test_linux_cpu_red_at_bundled_node_threshold():
    panel = asset_metric_panel(
        {"asset_id": "app-lab-01", "type": "Linux Server"},
        query_fn=_query({"node_cpu_seconds_total": 96.0, 'up{': 1.0, "node_memory": 10.0, "node_filesystem": 10.0}),
        range_fn=_no_spark,
    )
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["threshold"] == 95
    assert cpu["tone"] == "crit"


def test_missing_series_is_yellow_never_fake_zero():
    panel = asset_metric_panel(
        {"asset_id": "app-lab-01", "type": "Linux Server"},
        query_fn=_query({'up{': 1.0, "node_cpu_seconds_total": 12.0}),
        range_fn=_no_spark,
    )
    mem = next(tile for tile in panel["tiles"] if tile["key"] == "memory_percent")
    disk = next(tile for tile in panel["tiles"] if tile["key"] == "disk_percent")
    assert mem["value"] is None
    assert mem["tone"] == "warn"
    assert mem["display"] == "not collecting"
    assert mem["bar_pct"] is None
    assert disk["value"] is None
    assert disk["display"] != "0%"


def test_demo_asset_is_labeled_and_uses_demo_gauges():
    reset_demo_gauges()
    set_demo_cpu(12)
    set_demo_disk(35)
    panel = asset_metric_panel(
        {"asset_id": DEMO_ASSET, "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=lambda expr: {"error": "connection refused", "query": expr},
        range_fn=lambda expr: {"error": "down", "query": expr},
    )
    assert panel["demo"] is True
    assert panel["demo_label"] == "DEMO"
    assert panel["class"] == "demo"
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    disk = next(tile for tile in panel["tiles"] if tile["key"] == "disk_percent")
    mem = next(tile for tile in panel["tiles"] if tile["key"] == "memory_percent")
    assert cpu["value"] == 12.0
    assert cpu["threshold"] == 80
    assert cpu["tone"] == "ok"
    assert disk["value"] == 35.0
    assert mem["value"] is None
    assert mem["tone"] == "warn"
    reset_demo_gauges()


def test_demo_cpu_red_at_80():
    set_demo_cpu(80)
    panel = asset_metric_panel(
        {"asset_id": DEMO_ASSET, "type": "Linux Server"},
        query_fn=_query({'up{': 1.0}),
        range_fn=_no_spark,
    )
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["threshold"] == 80
    assert cpu["tone"] == "crit"
    reset_demo_gauges()


def test_prom_down_does_not_crash():
    panel = safe_asset_metric_panel(
        {"asset_id": "app-lab-01", "type": "Linux Server"},
        query_fn=lambda expr: {"error": "connection refused", "query": expr},
        range_fn=lambda expr: {"error": "connection refused", "query": expr},
    )
    assert panel["tiles"]
    assert panel["collecting"] is None
    assert "unreachable" in panel["collecting_line"].lower() or "not collecting" in panel["collecting_line"].lower()
    assert all(tile["tone"] == "warn" for tile in panel["tiles"])
    assert all(tile["value"] is None for tile in panel["tiles"])


def test_windows_tiles_use_windows_metrics_and_90_cpu():
    queries = promql_queries_for({"asset_id": "win-prod-01", "type": "Windows Server"})
    blob = str(queries)
    assert "windows_cpu_time_total" in blob
    assert "windows_os_physical_memory_free_bytes" in blob
    assert "node_cpu_seconds_total" not in blob
    panel = asset_metric_panel(
        {"asset_id": "win-prod-01", "type": "Windows Server", "monitoring_profile": "windows-standard"},
        query_fn=_query(
            {
                "windows_cpu_time_total": 91.0,
                "windows_os_physical_memory": 20.0,
                "windows_logical_disk_free": 30.0,
                'up{': 1.0,
            }
        ),
        range_fn=_no_spark,
    )
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["threshold"] == 90
    assert cpu["tone"] == "crit"
    assert panel["class"] == "windows"


def test_network_tile_is_up_only():
    panel = asset_metric_panel(
        {"asset_id": "edge-sw-01", "type": "Network Switch", "monitoring_profile": "network-switch"},
        query_fn=_query({'up{': 0.0}),
        range_fn=_no_spark,
    )
    assert [tile["key"] for tile in panel["tiles"]] == ["up"]
    assert panel["tiles"][0]["tone"] == "crit"
    assert panel["tiles"][0]["display"] == "down"
    assert "forgesre-snmp" in str(promql_queries_for({"asset_id": "edge-sw-01", "type": "Network Switch"}))


def test_unknown_class_is_yellow_not_collecting():
    panel = asset_metric_panel(
        {"asset_id": "mystery-01", "type": "Auto (detect exporter)"},
        query_fn=_query({"node_cpu": 5.0, 'up{': 1.0}),
        range_fn=_no_spark,
    )
    assert panel["class"] == "unknown"
    assert panel["collecting"] is False
    assert panel["tiles"][0]["tone"] == "warn"
    assert panel["tiles"][0]["display"] == "not collecting"


def test_sparkline_stays_small_when_range_exists():
    panel = asset_metric_panel(
        {"asset_id": "app-lab-01", "type": "Linux Server"},
        query_fn=_query({"node_cpu_seconds_total": 20.0, 'up{': 1.0, "node_memory": 20.0, "node_filesystem": 20.0}),
        range_fn=lambda _expr: {"values": [10.0, 20.0, 15.0, 22.0]},
    )
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["spark"]
    assert cpu["spark"].count(",") >= 3


def test_metrics_api_and_detail_html(monkeypatch):
    db = _db()
    create_manual_asset(
        db,
        hostname="app-lab-metrics",
        ip="10.44.77.91",
        type="Linux Server",
        actor="tester",
    )
    db.close()

    def fake_query(expr: str, timeout: float = 5.0) -> dict:
        del timeout
        if "node_cpu" in expr:
            return {"value": 22.0, "query": expr}
        if "node_memory" in expr:
            return {"value": 33.0, "query": expr}
        if "node_filesystem" in expr:
            return {"value": 44.0, "query": expr}
        if "up{" in expr or expr.strip() == "up":
            return {"value": 1.0, "query": expr}
        return {"value": None, "query": expr}

    monkeypatch.setattr("app.services.query_prometheus_expr", fake_query)
    monkeypatch.setattr("app.services.query_prometheus_range", lambda *args, **kwargs: {"values": []})

    client = _login("admin@forgesre.local")
    api = client.get("/api/v1/assets/app-lab-metrics/metrics")
    assert api.status_code == 200
    body = api.json()
    assert body["class"] == "linux"
    assert body["demo"] is False
    cpu = next(tile for tile in body["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["display"] == "22%"
    assert cpu["tone"] == "ok"

    page = client.get("/assets/app-lab-metrics")
    assert page.status_code == 200
    text = page.text
    assert "asset-detail-split" in text
    assert "Machine metrics" in text
    assert "Edit, clone, or remove" in text
    assert "asset-detail-actions" in text
    main_at = text.find("asset-detail-main")
    actions_at = text.find("asset-detail-actions")
    metrics_at = text.find("asset-detail-metrics")
    assert 0 <= main_at < actions_at < metrics_at
    assert "CPU" in text
    assert "22%" in text
    assert "Prometheus sees this target (up=1)." in text


def test_asset_detail_css_equal_columns():
    from pathlib import Path

    css = Path("frontend/static/app.css").read_text()
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in css
    assert "minmax(0, 1.35fr)" not in css


def test_demo_detail_page_has_demo_label_when_prom_down():
    reset_demo_gauges()
    client = _login("admin@forgesre.local")
    page = client.get(f"/assets/{DEMO_ASSET}")
    assert page.status_code == 200
    assert "DEMO" in page.text
    assert "Machine metrics" in page.text
    assert "asset-detail-metrics" in page.text
    win = client.get(f"/assets/{DEMO_WIN_ASSET}")
    assert win.status_code == 200
    assert "DEMO" in win.text
    sw = client.get(f"/assets/{DEMO_SW_ASSET}")
    assert sw.status_code == 200
    assert "DEMO" in sw.text


def test_metrics_api_survives_prometheus_down():
    reset_demo_gauges()
    client = _login("admin@forgesre.local")
    api = client.get(f"/api/v1/assets/{DEMO_ASSET}/metrics")
    assert api.status_code == 200
    body = api.json()
    assert body["demo"] is True
    assert body["tiles"]
    cpu = next(tile for tile in body["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["value"] == 12.0
    assert cpu["display"] == "12%"
    assert cpu["tone"] == "ok"


def test_viewer_can_read_metrics_api():
    db = _db()
    if db.query(User).filter_by(email="viewer-metrics@forgesre.local").first() is None:
        db.add(
            User(
                email="viewer-metrics@forgesre.local",
                name="View",
                password_hash=hash_password("testpass"),
                role="viewer",
            )
        )
        db.commit()
    db.close()
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "viewer-metrics@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    api = client.get(f"/api/v1/assets/{DEMO_ASSET}/metrics")
    assert api.status_code == 200
    page = client.get(f"/assets/{DEMO_ASSET}")
    assert page.status_code == 200
    assert "Machine metrics" in page.text
