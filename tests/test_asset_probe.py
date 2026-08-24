from app.asset_probe import (
    WINDOWS_EXPORTER_PORT,
    ad_hoc_item,
    asset_kind,
    default_scrape_address,
    format_report,
    hint_for,
    overall_exit,
    probe_target,
    select_assets,
)
from app.inventory import create_manual_asset, update_asset
from app.models import Asset
from app.seed import seed


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ping_ok(host, timeout):
    del host, timeout
    return 0, "rtt min/avg/max = 1.0/1.0/1.0 ms", ""


def _ping_fail(host, timeout):
    del host, timeout
    return 1, "", "1 packets transmitted, 0 received"


def _metrics_map(mapping):
    def fetch(url, timeout):
        del timeout
        if url in mapping:
            status, body = mapping[url]
            if status == 200:
                return 200, body, ""
            if status is None:
                return None, "", "timeout 2.0s"
            return status, body, f"HTTP {status}"
        return None, "", "timeout 2.0s"

    return fetch


def test_windows_type_is_not_linux_and_defaults_to_9182():
    assert asset_kind("Windows Server") == "windows"
    assert asset_kind("Linux Server") == "linux"
    assert default_scrape_address("Windows Server", "10.10.10.60") == "10.10.10.60:9182"
    assert default_scrape_address("Linux Server", "10.10.10.50") == "10.10.10.50:9100"
    assert default_scrape_address("Network device", "10.30.1.1") == ""


def test_create_windows_asset_scrapes_9182_linux_stays_9100():
    db = _db()
    win = create_manual_asset(
        db,
        hostname="ping-win-01",
        ip="10.89.11.60",
        type="Windows Server",
        actor="tester",
    )
    linux = create_manual_asset(
        db,
        hostname="ping-lnx-01",
        ip="10.89.11.50",
        type="Linux Server",
        actor="tester",
    )
    assert win.scrape_address == "10.89.11.60:9182"
    assert win.monitoring_profile == "windows-standard"
    assert linux.scrape_address == "10.89.11.50:9100"
    assert linux.monitoring_profile == "linux-standard"
    moved = update_asset(db, win, ip="10.89.11.61", actor="tester")
    assert moved.scrape_address == "10.89.11.61:9182"
    db.close()


def test_icmp_ok_metrics_fail_windows_hint():
    item = {
        "asset_id": "win-01",
        "hostname": "win-01",
        "ip": "10.10.10.60",
        "type": "Windows Server",
        "scrape_address": "10.10.10.60:9182",
    }
    result = probe_target(
        item,
        timeout=2,
        ping_runner=_ping_ok,
        metrics_fetcher=_metrics_map({}),
    )
    assert result.icmp.ok is True
    assert result.metrics.ok is False
    assert result.overall == "FAIL"
    assert ":9182" in result.metrics.detail
    assert "windows_exporter" in result.metrics.detail
    hint = hint_for(result)
    assert "ICMP ok" in hint or "ICMP" in hint
    assert "9182" in hint
    assert "firewall" in hint.lower() or "not running" in hint
    text = format_report([result], color=False)
    assert "FAIL" in text
    assert "L3" in text or "ICMP ping is L3" in text
    assert overall_exit([result]) == 1


def test_windows_wrong_port_also_tries_9182():
    item = {
        "asset_id": "win-01",
        "hostname": "win-01",
        "ip": "10.10.10.60",
        "type": "Windows Server",
        "scrape_address": "10.10.10.60:9100",
    }
    fetcher = _metrics_map(
        {
            "http://10.10.10.60:9100/metrics": (None, ""),
            "http://10.10.10.60:9182/metrics": (200, "# HELP windows_cpu_time_total\n"),
        }
    )
    result = probe_target(item, ping_runner=_ping_ok, metrics_fetcher=fetcher)
    assert result.metrics.ok is False
    assert result.extra
    assert result.extra[0].ok is True
    assert "Windows Server" in result.hint
    assert "9182" in result.hint


def test_linux_metrics_pass():
    item = {
        "asset_id": "app-01",
        "ip": "10.10.10.50",
        "type": "Linux Server",
        "scrape_address": "10.10.10.50:9100",
    }
    fetcher = _metrics_map({"http://10.10.10.50:9100/metrics": (200, "# HELP node_cpu_seconds_total\n")})
    result = probe_target(item, ping_runner=_ping_ok, metrics_fetcher=fetcher)
    assert result.overall == "PASS"
    assert "node_exporter" in result.metrics.detail
    assert overall_exit([result]) == 0


def test_network_device_skips_http():
    item = {
        "asset_id": "sw-01",
        "ip": "10.30.1.1",
        "type": "Network device",
        "scrape_address": "",
    }
    result = probe_target(item, ping_runner=_ping_ok, metrics_fetcher=_metrics_map({}))
    assert result.icmp.ok is True
    assert result.metrics.ok is None
    assert "SNMP" in result.metrics.detail
    assert result.overall == "WARN"


def test_ad_hoc_ip_probes_both_exporter_ports():
    item = ad_hoc_item("10.10.10.60")
    fetcher = _metrics_map(
        {
            "http://10.10.10.60:9100/metrics": (None, ""),
            "http://10.10.10.60:9182/metrics": (200, "# TYPE windows_cpu_time_total counter\n"),
        }
    )
    result = probe_target(item, ping_runner=_ping_fail, metrics_fetcher=fetcher)
    assert result.metrics.ok is True
    assert result.port == WINDOWS_EXPORTER_PORT
    assert result.kind == "windows"


def test_ad_hoc_prefers_windows_family_over_bare_9100():
    item = ad_hoc_item("10.10.10.62")
    fetcher = _metrics_map(
        {
            "http://10.10.10.62:9100/metrics": (200, "go_goroutines 1\n"),
            "http://10.10.10.62:9182/metrics": (200, "# TYPE windows_cpu_time_total counter\n"),
        }
    )
    result = probe_target(item, ping_runner=_ping_ok, metrics_fetcher=fetcher)
    assert result.kind == "windows"
    assert result.port == WINDOWS_EXPORTER_PORT
    assert result.metrics.ok is True


def test_select_skips_demo_unless_named():
    rows = [
        {"asset_id": "forge-demo-win-01", "ip": "10.10.10.21", "type": "Windows Server"},
        {"asset_id": "win-01", "ip": "10.10.10.60", "type": "Windows Server"},
    ]
    is_demo = lambda row: str(row.get("asset_id") or "").startswith("forge-demo-")
    chosen, skipped = select_assets(rows, is_demo=is_demo)
    assert [row["asset_id"] for row in chosen] == ["win-01"]
    assert skipped == 1
    named, skipped_named = select_assets(rows, "forge-demo-win-01", is_demo=is_demo)
    assert [row["asset_id"] for row in named] == ["forge-demo-win-01"]
    assert skipped_named == 0


def test_seeded_demo_windows_still_has_empty_scrape():
    db = _db()
    win = db.query(Asset).filter_by(asset_id="forge-demo-win-01").one()
    assert win.scrape_address == ""
    db.close()
