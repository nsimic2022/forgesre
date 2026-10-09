"""Device syslog into Loki, and incident evidence that does not borrow demo Core logs."""

from pathlib import Path

from rca.collector import collect_evidence_set, logql_selectors, loki_query_for

ROOT = Path(__file__).resolve().parents[1]


def test_logql_selectors_keep_or_inside_quotes():
    query = '{job="syslog",hostname="a or b"} or {job="syslog",ip="10.0.0.5"}'
    parts = logql_selectors(query)
    assert parts == [
        '{job="syslog",hostname="a or b"}',
        '{job="syslog",ip="10.0.0.5"}',
    ]


def test_real_asset_evidence_is_syslog_not_core_demo_lines():
    seen = []

    def log_fetcher(query, start, end):
        seen.append(query)
        if 'job="forgesre"' in query:
            return {"lines": ["core demo line forge-demo-01 container started"]}
        return {"lines": ["sw-edge-1 %LINK-3-UPDOWN: Interface down"]}

    items, limits = collect_evidence_set(
        incident={"number": "INC-9", "title": "link down"},
        asset={"asset_id": "sw-edge-1", "hostname": "sw-edge-1", "ip": "10.10.10.77", "type": "Network Switch"},
        alert={"alertname": "SnmpDeviceUnreachable"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"value": 0, "query": expr},
        log_fetcher=log_fetcher,
    )
    assert seen
    assert 'hostname="sw-edge-1"' in seen[0]
    assert 'ip="10.10.10.77"' in seen[0]
    assert seen[0].index("hostname=") < seen[0].index("ip=")
    logs = [item for item in items if item.type == "LOG"]
    assert logs
    assert logs[0].query == seen[0]
    assert logs[0].metadata.get("scope") == "syslog"
    blob = " ".join(str(item.content) for item in logs)
    assert "LINK-3-UPDOWN" in blob
    assert "core demo line" not in blob
    assert "forge-demo-01" not in blob
    assert all("DEMO" not in (item.metadata.get("label") or "") for item in logs)
    assert not any("appliance" in item for item in limits)


def test_unmatched_incident_does_not_attach_demo_core_logs():
    seen = []

    def log_fetcher(query, start, end):
        seen.append(query)
        return {"lines": ["core demo line forge-demo-01"]}

    items, _limits = collect_evidence_set(
        incident={"number": "INC-2", "title": "unlabeled"},
        asset={},
        alert={"alertname": "HighCPU"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"value": 1, "query": expr},
        log_fetcher=log_fetcher,
    )
    assert seen == []
    assert loki_query_for({}) is None
    assert not any(item.type == "LOG" for item in items)
    blob = " ".join(item.query + str(item.content) for item in items)
    assert "forge-demo-01" not in blob
    assert 'job="forgesre"' not in blob


def test_demo_asset_still_uses_core_log_stream():
    items, limits = collect_evidence_set(
        incident={"number": "INC-1", "title": "High CPU"},
        asset={"asset_id": "forge-demo-01", "hostname": "forge-demo-01", "ip": "10.10.10.20"},
        alert={"alertname": "HighCPU"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=lambda expr: {"value": 94, "query": expr},
        log_fetcher=lambda query, start, end: {"lines": [f"core log {query}"]},
    )
    logs = [item for item in items if item.type == "LOG"]
    assert logs
    assert logs[0].query == '{job="forgesre"}'
    assert "core log" in str(logs[0].content)
    assert any("DEMO" in item for item in limits)


def test_alloy_config_has_syslog_receiver_and_core_pipeline():
    text = (ROOT / "logging" / "config.alloy").read_text(encoding="utf-8")
    assert 'loki.source.syslog "devices"' in text
    assert 'address                = "0.0.0.0:514"' in text
    assert 'protocol               = "udp"' in text
    assert 'protocol               = "tcp"' in text
    assert 'syslog_format          = "rfc3164"' in text
    assert 'job = "syslog"' in text
    assert 'target_label  = "hostname"' in text
    assert 'target_label  = "host"' in text
    assert 'target_label  = "ip"' in text
    assert 'replacement   = "unknown"' in text
    assert 'loki.source.file "core"' in text
    assert "/var/log/forgesre/*.log" in text
    assert 'asset    = "forge-demo-01"' in text
    assert 'job      = "forgesre"' in text
    assert "node_exporter" not in text
    assert "snmp_exporter" not in text
    assert "9100" not in text
    assert "9116" not in text


def test_loki_config_is_local_with_retention():
    text = (ROOT / "logging" / "loki.yml").read_text(encoding="utf-8")
    assert "http_listen_address: 127.0.0.1" in text
    assert "grpc_listen_address: 127.0.0.1" in text
    assert "0.0.0.0" not in text
    assert "retention_period: 168h" in text
    assert "retention_enabled: true" in text
    assert "ingestion_rate_mb: 16" in text


def test_render_monitoring_copies_alloy_and_loki():
    text = (ROOT / "scripts" / "render-monitoring.sh").read_text(encoding="utf-8")
    assert "logging/config.alloy" in text
    assert "logging/loki.yml" in text
    assert "ALLOY_CONFIG" in text
    assert "LOKI_CONFIG" in text
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ALLOY_CONFIG" in compose
    assert "LOKI_CONFIG" in compose
    assert "0.0.0.0:514" not in compose
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ALLOY_CONFIG=" in example
    assert "LOKI_CONFIG=" in example


def test_doctor_loki_ready_and_alloy_syslog_listen(monkeypatch):
    from app.api import doctor_payload

    from app.settings import settings

    seen = []

    def _http(url, method):
        seen.append(url)
        return {"status": "ok"}

    monkeypatch.setattr("app.api._http", _http)
    monkeypatch.setattr("app.api.syslog_listen_ports", lambda port=514: {"udp": False, "tcp": True})
    payload = doctor_payload(force=True)
    assert f"{settings.loki_url.rstrip('/')}/ready" in seen
    assert not any("buildinfo" in url for url in seen)
    alloy = payload["components"]["alloy"]
    assert alloy["status"] == "warn"
    assert "UDP/514" in (alloy.get("why") or "")
    assert "alloy" not in payload["failed"]

    monkeypatch.setattr("app.api.syslog_listen_ports", lambda port=514: {"udp": True, "tcp": True})
    payload = doctor_payload(force=True)
    assert payload["components"]["alloy"]["status"] == "ok"
    assert "514" in (payload["components"]["alloy"].get("why") or "")
    assert payload["components"]["loki"]["status"] == "ok"
    assert "ready" in (payload["components"]["loki"].get("why") or "").lower()


def test_query_loki_runs_hostname_selector_before_ip(monkeypatch):
    from app import services as svc

    order = []

    def fake(query, limit, start, end):
        order.append(query)
        if "hostname" in query:
            return ["from-hostname"]
        return ["from-ip"]

    monkeypatch.setattr(svc, "_query_loki_selector", fake)
    lines = svc.query_loki(query='{job="syslog",hostname="db-01"} or {job="syslog",ip="10.1.1.8"}', limit=10)
    assert order[0].startswith("{job=\"syslog\",hostname=")
    assert "ip=" in order[1]
    assert lines == ["from-hostname", "from-ip"]


def test_collect_evidence_real_asset_does_not_store_demo_lines(monkeypatch):
    from app.db import Base, SessionLocal, engine
    from app.models import Asset, Evidence, Incident
    from app.seed import seed
    from app.services import collect_evidence

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    asset = Asset(asset_id="sw-syslog-1", hostname="sw-syslog-1", ip="10.10.10.77", type="Network Switch")
    db.add(asset)
    db.commit()
    db.refresh(asset)
    incident = Incident(
        number="INC-SYSLOG-TEST",
        title="link",
        fingerprint="syslog-test:sw-syslog-1",
        asset_id=asset.id,
        alert_payload={"labels": {"alertname": "SnmpDeviceUnreachable"}},
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    def fake_loki(limit=20, query="", start=None, end=None):
        if 'job="forgesre"' in (query or ""):
            return ["core demo line forge-demo-01"]
        assert 'hostname="sw-syslog-1"' in query
        assert 'ip="10.10.10.77"' in query
        return ["sw-syslog-1 %LINK-3-UPDOWN"]

    monkeypatch.setattr("app.services.query_loki", fake_loki)
    monkeypatch.setattr("app.services.query_prometheus", lambda asset=None: {"queries": {}, "up": 0})
    collect_evidence(db, incident, {"alertname": "SnmpDeviceUnreachable", "labels": {}})
    db.refresh(incident)
    blob = " ".join((ev.query or "") + str(ev.payload) for ev in db.query(Evidence).filter_by(incident_id=incident.id))
    assert "LINK-3-UPDOWN" in blob
    assert "core demo line" not in blob
    assert '{job="forgesre"}' not in blob
    db.close()
