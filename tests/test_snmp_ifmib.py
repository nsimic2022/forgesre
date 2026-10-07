"""snmp_exporter v0.26 module shape for monitoring/snmp.yml(.tpl): no module-level ``lookups``,
every module has ``metrics:``, the ifAdminStatus/ifOperStatus series the alerts and asset verify use,
and the bundled auths plus forgesre-asset-auths markers untouched. ``--dry-run`` runs when an
snmp_exporter binary (PATH or SNMP_EXPORTER_BIN) or Docker with prom/snmp-exporter:v0.26.0 is present."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = ("snmp.yml", "snmp.yml.tpl")
IMAGE = "prom/snmp-exporter:v0.26.0"
# Keys a v0.26 module accepts (config.Module + inline WalkParams); anything else fails to load.
MODULE_KEYS = {"walk", "get", "metrics", "max_repetitions", "retries", "timeout", "use_unconnected_udp_socket", "filters"}
IF_COLUMNS = {
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifAdminStatus": "1.3.6.1.2.1.2.2.1.7",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "ifName": "1.3.6.1.2.1.31.1.1.1.1",
    "ifHCInOctets": "1.3.6.1.2.1.31.1.1.1.6",
    "ifHCOutOctets": "1.3.6.1.2.1.31.1.1.1.10",
    "ifAlias": "1.3.6.1.2.1.31.1.1.1.18",
}


def _text(name: str) -> str:
    return (ROOT / "monitoring" / name).read_text(encoding="utf-8").replace("__SNMP_COMMUNITY__", "public")


def _parsed(name: str) -> dict:
    return yaml.safe_load(_text(name))


@pytest.mark.parametrize("name", FILES)
def test_modules_have_metrics_and_no_module_level_lookups(name):
    modules = _parsed(name)["modules"]
    assert modules
    for mod_name, module in modules.items():
        assert "lookups" not in module, f"{name}: module {mod_name} has module-level lookups (removed in v0.26)"
        assert set(module) <= MODULE_KEYS, f"{name}: module {mod_name} has unknown keys {set(module) - MODULE_KEYS}"
        assert module.get("metrics"), f"{name}: module {mod_name} has no metrics:"


@pytest.mark.parametrize("name", FILES)
def test_if_mib_slice_walks_planned_columns_with_per_metric_lookups(name):
    module = _parsed(name)["modules"]["if_mib"]
    assert set(module["walk"]) == set(IF_COLUMNS.values())
    assert module["get"] == ["1.3.6.1.2.1.1.3.0"]
    metrics = {m["name"]: m for m in module["metrics"]}
    assert set(metrics) == {"sysUpTime", "ifAdminStatus", "ifOperStatus", "ifHCInOctets", "ifHCOutOctets"}
    assert metrics["sysUpTime"]["oid"] == "1.3.6.1.2.1.1.3" and "indexes" not in metrics["sysUpTime"]
    walked = set(module["walk"])
    for name_ in ("ifAdminStatus", "ifOperStatus", "ifHCInOctets", "ifHCOutOctets"):
        metric = metrics[name_]
        assert metric["oid"] == IF_COLUMNS[name_]
        assert metric["indexes"] == [{"labelname": "ifIndex", "type": "gauge"}]
        labels = {lk["labelname"]: lk for lk in metric["lookups"]}
        assert set(labels) == {"ifAlias", "ifDescr", "ifName"}
        for label, lk in labels.items():
            assert lk["oid"] == IF_COLUMNS[label] and lk["oid"] in walked
            assert lk["labels"] == ["ifIndex"] and lk["type"] == "DisplayString"
    assert metrics["ifAdminStatus"]["type"] == metrics["ifOperStatus"]["type"] == "gauge"
    assert metrics["ifOperStatus"]["enum_values"][2] == "down"
    assert metrics["ifAdminStatus"]["enum_values"][1] == "up"


def test_alerts_and_asset_verify_series_are_produced():
    alerts = (ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8")
    assert "ifOperStatus == 2 and ifAdminStatus == 1" in alerts
    assert "ifName" in alerts
    produced = {m["name"] for m in _parsed("snmp.yml")["modules"]["if_mib"]["metrics"]}
    verify = (ROOT / "backend" / "app" / "asset_verify.py").read_text(encoding="utf-8")
    for series in ("ifOperStatus", "ifHCInOctets", "sysUpTime", "ifAdminStatus"):
        assert series in verify and series in produced


@pytest.mark.parametrize("name", FILES)
def test_auths_and_markers_untouched(name):
    text = (ROOT / "monitoring" / name).read_text(encoding="utf-8")
    assert "# forgesre-asset-auths begin" in text and "# forgesre-asset-auths end" in text
    assert text.index("# forgesre-asset-auths end") < text.index("\nmodules:\n")
    auths = _parsed(name)["auths"]
    assert auths == {"public_v2": {"version": 2, "community": "public"}, "public_v1": {"version": 1, "community": "public"}}
    if name.endswith(".tpl"):
        assert text.count("community: __SNMP_COMMUNITY__") == 2


def test_template_and_committed_snmp_yml_match():
    assert _text("snmp.yml.tpl") == _text("snmp.yml")


def test_render_monitoring_always_reloads_snmp_exporter():
    render = (ROOT / "scripts" / "render-monitoring.sh").read_text(encoding="utf-8")
    assert "reload=False" in render.split("render_snmp_auths.apply", 1)[1].split("\n", 1)[0]
    shell_tail = render.split("\nPY\n", 1)[1]
    assert "http://127.0.0.1:9116/-/reload" in shell_tail
    assert "docker compose restart snmp-exporter" in shell_tail
    assert "install.sh" not in shell_tail
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "render-monitoring.sh")], check=True)


def test_apply_without_reload_does_not_call_exporter(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import render_snmp_auths as r

    target = tmp_path / "snmp.yml"
    target.write_text(_text("snmp.yml.tpl"))
    calls: list[str] = []
    monkeypatch.setattr(r, "fetch_auths", lambda port, token, timeout=5.0: {"forgesre_x": {"version": 2, "community": "c"}})
    monkeypatch.setattr(r, "reload_exporter", lambda url="": calls.append(url) or True)
    status = r.apply(target, "8080", "tok", reload=False)
    assert calls == [] and "reloaded" not in status
    parsed = yaml.safe_load(target.read_text())
    assert parsed["modules"]["if_mib"]["metrics"]
    assert "lookups" not in parsed["modules"]["if_mib"]


def _dry_run_cmd(config: Path) -> list[str] | None:
    binary = os.environ.get("SNMP_EXPORTER_BIN") or shutil.which("snmp_exporter")
    if binary:
        return [binary, f"--config.file={config}", "--dry-run"]
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return [docker, "run", "--rm", "-v", f"{config}:/etc/snmp_exporter/snmp.yml:ro", IMAGE,
                "--config.file=/etc/snmp_exporter/snmp.yml", "--dry-run"]
    return None


@pytest.mark.parametrize("name", FILES)
def test_snmp_exporter_dry_run(name, tmp_path):
    config = tmp_path / "snmp.yml"
    config.write_text(_text(name))
    cmd = _dry_run_cmd(config)
    if cmd is None:
        pytest.skip("no snmp_exporter binary (SNMP_EXPORTER_BIN / PATH) or local prom/snmp-exporter:v0.26.0 image")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration parsed successfully" in result.stdout + result.stderr
