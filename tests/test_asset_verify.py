from pathlib import Path

from app.asset_verify import (
    classify_verify,
    compose_verify,
    format_verify_report,
    parse_fact_metrics,
    verify_exit,
    verify_target,
)
from app.cli_ops import main
from app.asset_probe import AssetProbe, CheckResult


def _ping_ok(host, timeout):
    del host, timeout
    return 0, "rtt min/avg/max = 1.0/1.0/1.0 ms", ""


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


def test_classify_unknown_is_skip_class():
    vclass, reason = classify_verify({"type": "Web/appliance", "ip": "10.1.1.8"})
    assert vclass == "unknown"
    assert "inventory-only" in reason or "No exporter class" in reason or "no exporter" in reason.lower()
    auto, auto_reason = classify_verify({"type": "Auto (detect exporter)", "ip": "10.1.1.9"})
    assert auto == "unknown"
    assert "Auto" in auto_reason


def test_linux_node_family_and_prom_up_is_pass():
    item = {
        "asset_id": "app-01",
        "hostname": "app-01",
        "ip": "10.10.10.50",
        "type": "Linux Server",
        "scrape_address": "10.10.10.50:9100",
        "monitoring_profile": "linux-standard",
    }
    fetcher = _metrics_map(
        {"http://10.10.10.50:9100/metrics": (200, "# HELP node_cpu_seconds_total\nnode_cpu_seconds_total 1\n")}
    )
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=fetcher,
        in_http_sd=True,
        query_fn=lambda expr: {"value": 1.0, "query": expr},
        ai_enabled=False,
    )
    assert report.vclass == "linux"
    assert report.family.ok is True
    assert "node_" in report.family.detail
    assert report.prom.ok is True
    assert report.overall == "PASS"
    assert report.llm.ok is None
    assert "ForgeAI disabled" in report.llm.detail
    assert verify_exit([report]) == 0
    text = format_verify_report([report], color=False, detail=True)
    assert "=== Inventory ===" in text
    assert "PASS" in text
    assert "not ./forgesre test" in text.lower()


def test_missing_exporter_is_fail_or_skip_never_fake_pass():
    item = {
        "asset_id": "ghost-01",
        "ip": "10.10.10.99",
        "type": "Linux Server",
        "scrape_address": "10.10.10.99:9100",
    }
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=_metrics_map({}),
        in_http_sd=False,
        query_fn=lambda expr: {"value": None, "query": expr},
    )
    assert report.overall != "PASS"
    assert report.overall in {"FAIL", "SKIP"}
    assert report.port.ok is False or report.prom.ok is None


def test_unknown_class_overall_skip():
    item = {"asset_id": "web-01", "ip": "10.1.1.8", "type": "Web/appliance", "scrape_address": ""}
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=_metrics_map({}),
        in_http_sd=False,
    )
    assert report.vclass == "unknown"
    assert report.overall == "SKIP"
    assert report.port.ok is None
    assert verify_exit([report]) == 0


def test_demo_lab_never_counts_as_real_scrape():
    item = {
        "asset_id": "forge-demo-01",
        "hostname": "forge-demo-01",
        "ip": "10.10.10.20",
        "type": "Linux Server",
        "scrape_address": "10.10.10.20:9100",
    }
    fetcher = _metrics_map({"http://10.10.10.20:9100/metrics": (200, "node_uname_info 1\n")})
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=fetcher,
        in_http_sd=True,
        query_fn=lambda expr: {"value": 1.0, "query": expr},
    )
    assert report.lab is True
    assert report.overall == "SKIP"
    assert "DEMO" in report.overall_reason or "lab" in report.overall_reason.lower()
    assert report.prom.ok is None
    text = format_verify_report([report], color=False, detail=True)
    assert "DEMO" in text or "lab" in text.lower()


def test_windows_family_mismatch_is_fail():
    item = {
        "asset_id": "win-01",
        "ip": "10.10.10.60",
        "type": "Windows Server",
        "scrape_address": "10.10.10.60:9182",
    }
    fetcher = _metrics_map(
        {"http://10.10.10.60:9182/metrics": (200, "# HELP node_cpu_seconds_total\nnode_uname_info 1\n")}
    )
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=fetcher,
        in_http_sd=True,
        query_fn=lambda expr: {"value": 1.0, "query": expr},
    )
    assert report.vclass == "windows"
    assert report.family.ok is False
    assert report.overall == "FAIL"
    assert verify_exit([report]) == 1


def test_network_snmp_pass_when_prom_up():
    item = {
        "asset_id": "sw-01",
        "ip": "10.30.1.1",
        "type": "Network device",
        "scrape_address": "",
    }
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=_metrics_map({}),
        snmp_prober=lambda host, timeout=0.4: True,
        in_snmp_sd=True,
        query_fn=lambda expr: {"value": 1.0, "query": expr},
    )
    assert report.vclass == "network"
    assert report.port.ok is True
    assert report.family.ok is True
    assert report.prom.ok is True
    assert report.overall == "PASS"


def test_prom_empty_up_is_skip_not_pass():
    item = {
        "asset_id": "app-02",
        "ip": "10.10.10.51",
        "type": "Linux Server",
        "scrape_address": "10.10.10.51:9100",
    }
    fetcher = _metrics_map({"http://10.10.10.51:9100/metrics": (200, "node_cpu_seconds_total 1\n")})
    report = verify_target(
        item,
        ping_runner=_ping_ok,
        metrics_fetcher=fetcher,
        in_http_sd=True,
        query_fn=lambda expr: {"value": None, "query": expr},
    )
    assert report.port.ok is True
    assert report.prom.ok is None
    assert report.overall == "SKIP"


def test_rca_mismatch_vs_promql():
    probe = AssetProbe(
        asset_id="app-01",
        hostname="app-01",
        ip="10.10.10.50",
        kind="linux",
        type="Linux Server",
        scrape="10.10.10.50:9100",
        port=9100,
        icmp=CheckResult("icmp", True, "reachable"),
        metrics=CheckResult(
            "metrics",
            True,
            "node_exporter :9100/metrics prometheus text",
            preview="# HELP node_cpu_seconds_total\nnode_cpu_seconds_total 1\n",
        ),
    )
    item = {
        "asset_id": "app-01",
        "hostname": "app-01",
        "ip": "10.10.10.50",
        "type": "Linux Server",
        "scrape_address": "10.10.10.50:9100",
    }
    report = compose_verify(
        item,
        probe,
        in_http_sd=True,
        query_fn=lambda expr: {"value": 1.0, "query": expr},
        rca={
            "incident": "INC-0001",
            "facts": [{"text": "cpu_percent is 94.0%."}],
            "provider": "builtin-analyst",
        },
        live_metrics={"cpu_percent": 12.0, "up": 1.0},
        ai_enabled=True,
    )
    assert report.rca.ok is False
    assert "cpu_percent" in report.rca.detail
    assert report.llm.ok is None


def test_parse_fact_metrics():
    found = parse_fact_metrics([{"text": "cpu_percent is 94.0%."}, {"text": "Alert HighCPU is firing."}])
    assert found["cpu_percent"] == 94.0


def test_cli_ops_dispatches_verify(monkeypatch):
    seen = {}

    def fake(port, args):
        seen["port"] = port
        seen["args"] = list(args)
        raise SystemExit(0)

    monkeypatch.setattr("app.cli_ops.cmd_verify", fake)
    try:
        main(["8080", "verify", "win-01"])
    except SystemExit as exc:
        assert exc.code == 0
    assert seen["port"] == "8080"
    assert seen["args"] == ["win-01"]


def test_cli_ops_unknown_command_does_not_alias_verify():
    try:
        main(["8080", "not-verify"])
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "unknown" in str(exc).lower()


def test_demo_ids_helper_has_no_orm():
    from app.demo_ids import DEMO_ASSET_PREFIX, is_demo_asset_id

    assert DEMO_ASSET_PREFIX == "forge-demo-"
    assert is_demo_asset_id("forge-demo-01")
    assert is_demo_asset_id("FORGE-DEMO-win-01")
    assert not is_demo_asset_id("app-01")
    assert not is_demo_asset_id("")
    assert not is_demo_asset_id(None)


def _assert_no_sqlalchemy_or_seed_import(rel: str) -> None:
    import ast

    source = (Path(__file__).resolve().parents[1] / "backend" / "app" / rel).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sqlalchemy"), f"{rel} imports {alias.name}"
                assert alias.name != "app.seed", rel
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("sqlalchemy"), f"{rel} imports {mod}"
            assert mod != "app.seed", f"{rel} imports app.seed"
            if mod == "app":
                for alias in node.names:
                    assert alias.name != "seed", f"{rel} imports seed from app"


def test_host_verify_modules_do_not_import_seed_or_sqlalchemy():
    _assert_no_sqlalchemy_or_seed_import("demo_ids.py")
    _assert_no_sqlalchemy_or_seed_import("asset_verify.py")
    _assert_no_sqlalchemy_or_seed_import("cli_ops.py")
    _assert_no_sqlalchemy_or_seed_import("cli_view.py")
    _assert_no_sqlalchemy_or_seed_import("asset_probe.py")
    _assert_no_sqlalchemy_or_seed_import("exporter_detect.py")


def test_importing_asset_verify_and_cli_ops_does_not_import_sqlalchemy():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    blocker = r"""
import builtins
import sys

real = builtins.__import__


def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "sqlalchemy" or name.startswith("sqlalchemy."):
        raise ModuleNotFoundError(name)
    if name == "app.seed" or (name == "app" and fromlist and "seed" in fromlist):
        raise ModuleNotFoundError("app.seed")
    return real(name, globals, locals, fromlist, level)


builtins.__import__ = blocked
sys.path.insert(0, "backend")
from app import asset_verify
from app import cli_ops
from app.demo_ids import is_demo_asset_id

assert is_demo_asset_id("forge-demo-01")
assert hasattr(asset_verify, "verify_target")
assert hasattr(cli_ops, "cmd_verify")
print("ok")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "backend")
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    assert "sqlalchemy" not in result.stderr.lower()
    assert "Traceback" not in result.stderr
