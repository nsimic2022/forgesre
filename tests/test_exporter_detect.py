from discovery import classify, probe_host

from app.exporter_detect import (
    AUTO_ASSET_TYPE,
    classify_exporter_metrics,
    detect_exporter,
    is_auto_asset_type,
)
from app.inventory import approve_candidate, create_manual_asset, update_asset, upsert_candidate
from app.seed import seed


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _fetch_map(mapping):
    def fetch(url, timeout):
        del timeout
        if url in mapping:
            status, body = mapping[url]
            if status == 200:
                return 200, body, ""
            if status is None:
                return None, "", "timeout 1.0s"
            return status, body, f"HTTP {status}"
        return None, "", "timeout 1.0s"

    return fetch


def test_classify_metrics_windows_and_linux():
    assert classify_exporter_metrics("# HELP windows_cpu_time_total\nwindows_cpu_time_total 1") == "windows"
    assert classify_exporter_metrics("# TYPE node_cpu_seconds_total counter\nnode_cpu_seconds_total 1") == "linux"
    assert classify_exporter_metrics("# HELP node_uname_info\nnode_uname_info 1") == "linux"
    assert classify_exporter_metrics("go_goroutines 7") == ""
    assert is_auto_asset_type(AUTO_ASSET_TYPE) is True
    assert is_auto_asset_type("Linux Server") is False


def test_detect_windows_only():
    fetcher = _fetch_map(
        {
            "http://10.44.1.60:9182/metrics": (200, "# HELP windows_cpu_time_total\nwindows_cpu_time_total 1\n"),
            "http://10.44.1.60:9100/metrics": (None, ""),
        }
    )
    result = detect_exporter("10.44.1.60", fetcher=fetcher)
    assert result.kind == "windows"
    assert result.asset_type == "Windows Server"
    assert result.scrape_address == "10.44.1.60:9182"
    assert result.profile == "windows-standard"
    assert "9182" in result.message


def test_detect_linux_only():
    fetcher = _fetch_map(
        {
            "http://10.44.1.50:9100/metrics": (200, "# HELP node_uname_info\nnode_cpu_seconds_total 1\n"),
            "http://10.44.1.50:9182/metrics": (None, ""),
        }
    )
    result = detect_exporter("10.44.1.50", fetcher=fetcher)
    assert result.kind == "linux"
    assert result.scrape_address == "10.44.1.50:9100"


def test_detect_both_prefers_saved_type_then_windows():
    fetcher = _fetch_map(
        {
            "http://10.44.1.70:9182/metrics": (200, "windows_cpu_time_total 1\n"),
            "http://10.44.1.70:9100/metrics": (200, "node_uname_info 1\nnode_cpu_seconds_total 1\n"),
        }
    )
    kept = detect_exporter("10.44.1.70", hint_type="Linux Server", fetcher=fetcher)
    assert kept.kind == "linux"
    assert kept.tie_break == "saved-type"
    prefer = detect_exporter("10.44.1.70", fetcher=fetcher)
    assert prefer.kind == "windows"
    assert prefer.tie_break == "windows-over-node"


def test_detect_neither_does_not_assume_linux():
    result = detect_exporter("10.44.1.80", fetcher=_fetch_map({}), snmp_ok=False)
    assert result.kind == ""
    assert result.scrape_address == ""
    assert "ICMP" in result.message
    assert "not a scrape" in result.message.lower() or "ICMP ping is not a scrape" in result.message


def test_detect_snmp_marks_network_not_http_guess():
    result = detect_exporter("10.44.1.90", fetcher=_fetch_map({}), snmp_ok=True)
    assert result.kind == "network"
    assert result.asset_type == "Network device"
    assert result.scrape_address == ""
    assert result.profile == "network-switch"
    assert result.tie_break == "snmp-udp"
    assert result.snmp is True


def test_detect_does_not_snmp_override_saved_linux():
    result = detect_exporter(
        "10.44.1.91",
        hint_type="Linux Server",
        fetcher=_fetch_map({}),
        snmp_ok=True,
    )
    assert result.kind == ""


def test_windows_http_wins_over_snmp():
    fetcher = _fetch_map(
        {
            "http://10.44.1.92:9182/metrics": (200, "windows_cpu_time_total 1\n"),
            "http://10.44.1.92:9100/metrics": (None, ""),
        }
    )
    result = detect_exporter("10.44.1.92", fetcher=fetcher, snmp_ok=True)
    assert result.kind == "windows"
    assert result.scrape_address == "10.44.1.92:9182"


def test_auto_create_uses_windows_exporter():
    db = _db()
    fetcher = _fetch_map(
        {
            "http://10.44.2.60:9182/metrics": (200, "windows_cs_hostname 1\nwindows_cpu_time_total 1\n"),
            "http://10.44.2.60:9100/metrics": (None, ""),
        }
    )
    asset = create_manual_asset(
        db,
        hostname="auto-win-01",
        ip="10.44.2.60",
        type=AUTO_ASSET_TYPE,
        actor="tester",
        metrics_fetcher=fetcher,
    )
    assert asset.type == "Windows Server"
    assert asset.scrape_address == "10.44.2.60:9182"
    assert asset.monitoring_profile == "windows-standard"
    db.close()


def test_auto_create_neither_leaves_type_unset():
    db = _db()
    asset = create_manual_asset(
        db,
        hostname="auto-none-01",
        ip="10.44.2.80",
        type=AUTO_ASSET_TYPE,
        actor="tester",
        metrics_fetcher=_fetch_map({}),
    )
    assert asset.type == "Unknown"
    assert asset.scrape_address == ""
    assert "linux-standard" not in (asset.monitoring_profile or "")
    db.close()


def test_auto_create_network_from_snmp_not_http():
    db = _db()
    asset = create_manual_asset(
        db,
        hostname="auto-sw-01",
        ip="10.44.2.91",
        type=AUTO_ASSET_TYPE,
        actor="tester",
        metrics_fetcher=_fetch_map({}),
        snmp_ok=True,
    )
    assert asset.type == "Network device"
    assert asset.scrape_address == ""
    assert asset.monitoring_profile == "network-switch"
    db.close()


def test_explicit_linux_still_defaults_9100_without_http():
    db = _db()
    asset = create_manual_asset(
        db,
        hostname="explicit-lnx-01",
        ip="10.44.2.50",
        type="Linux Server",
        actor="tester",
        metrics_fetcher=_fetch_map({}),
    )
    assert asset.type == "Linux Server"
    assert asset.scrape_address == "10.44.2.50:9100"
    db.close()


def test_detect_button_rewrites_linux_row_to_windows():
    db = _db()
    asset = create_manual_asset(
        db,
        hostname="was-linux-01",
        ip="10.44.2.61",
        type="Linux Server",
        actor="tester",
    )
    assert asset.scrape_address == "10.44.2.61:9100"
    fetcher = _fetch_map(
        {
            "http://10.44.2.61:9182/metrics": (200, "windows_cpu_time_total 1\n"),
            "http://10.44.2.61:9100/metrics": (None, ""),
        }
    )
    updated = update_asset(db, asset, detect=True, actor="tester", metrics_fetcher=fetcher)
    assert updated.type == "Windows Server"
    assert updated.scrape_address == "10.44.2.61:9182"
    assert updated.monitoring_profile == "windows-standard"
    db.close()


def test_classify_exporter_kind_and_tcp_fallback():
    assert classify([9100, 9182]) == "Possible Linux server"
    assert classify([9100, 9182], exporter_kind="windows") == "Possible Windows server"
    assert classify([9100], exporter_kind="none") == "Possible Linux server (TCP 9100, no node_exporter /metrics)"
    assert "pick OS" in classify([9100, 9182], exporter_kind="none")
    assert classify([22], snmp_ok=True, exporter_kind="network") == "Possible network device"
    assert classify([161], exporter_kind="network") == "Possible network device"


def test_probe_host_uses_http_family_not_tcp_9100():
    def fetch(url, timeout):
        del timeout
        if ":9182/" in url:
            return 200, "windows_cpu_time_total 1\n", ""
        return None, "", "timeout"

    import discovery as discovery_mod

    orig_tcp = discovery_mod._open_tcp
    orig_snmp = discovery_mod.probe_snmp_udp

    def fake_tcp(ip, port, timeout):
        del ip, timeout
        return port in {22, 9100, 9182}

    discovery_mod._open_tcp = fake_tcp
    discovery_mod.probe_snmp_udp = lambda ip, timeout=0.4: False
    try:
        result = probe_host("10.44.3.60", metrics_fetcher=fetch)
    finally:
        discovery_mod._open_tcp = orig_tcp
        discovery_mod.probe_snmp_udp = orig_snmp
    assert result["proposed_role"] == "Possible Windows server"
    assert result["exporter_kind"] == "windows"
    assert 9182 in result["open_ports"]


def test_probe_host_snmp_only_is_network_not_http_guess():
    import discovery as discovery_mod

    orig_tcp = discovery_mod._open_tcp
    orig_snmp = discovery_mod.probe_snmp_udp
    discovery_mod._open_tcp = lambda ip, port, timeout: False
    discovery_mod.probe_snmp_udp = lambda ip, timeout=0.4: True
    try:
        result = probe_host("10.44.3.70", metrics_fetcher=_fetch_map({}))
    finally:
        discovery_mod._open_tcp = orig_tcp
        discovery_mod.probe_snmp_udp = orig_snmp
    assert result["proposed_role"] == "Possible network device"
    assert result["exporter_kind"] == "network"
    assert result["snmp_ok"] is True
    assert 161 in result["open_ports"]


def test_approve_tcp_only_9100_does_not_scrape():
    db = _db()
    row = upsert_candidate(
        db,
        "10.44.3.90",
        "Possible Linux server (TCP 9100, no node_exporter /metrics)",
        [22, 9100],
    )
    db.commit()
    asset = approve_candidate(db, row, actor="tester")
    assert asset.type == "Linux Server"
    assert asset.scrape_address == ""
    db.close()


def test_api_detect_and_auto_form(monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.exporter_detect import ExporterDetect
    from app.main import app
    from app.models import User
    from app.security import hash_password

    def fake_detect(ip, hint_type="", hint_profile="", timeout=1.0, fetcher=None, **kwargs):
        del hint_type, hint_profile, timeout, fetcher, kwargs
        return ExporterDetect(
            kind="windows",
            asset_type="Windows Server",
            scrape_address=f"{ip}:9182",
            profile="windows-standard",
            port=9182,
            windows=True,
            message=f"Detected windows_exporter on :9182. Type Windows Server, scrape {ip}:9182.",
            role="Possible Windows server",
        )

    monkeypatch.setattr("app.inventory.detect_exporter", fake_detect)
    monkeypatch.setattr("app.exporter_detect.detect_exporter", fake_detect)
    monkeypatch.setattr("app.api.detect_exporter", fake_detect)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.add(
        User(
            email="detect-ui@forgesre.local",
            name="Det",
            password_hash=hash_password("testpass"),
            role="analyst",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "detect-ui@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    api = client.get("/api/v1/detect-exporter", params={"ip": "10.44.4.60"})
    assert api.status_code == 200
    body = api.json()
    assert body["kind"] == "windows"
    assert body["scrape_address"] == "10.44.4.60:9182"
    created = client.post(
        "/assets",
        data={
            "hostname": "auto-ui-win",
            "ip": "10.44.4.61",
            "type": AUTO_ASSET_TYPE,
            "environment": "Production",
            "owner": "payments",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    assert "notice=" in (created.headers.get("location") or "")
    detail = client.get("/assets/auto-ui-win")
    assert detail.status_code == 200
    assert b"10.44.4.61:9182" in detail.content
    assert b"Windows Server" in detail.content
    db.close()
