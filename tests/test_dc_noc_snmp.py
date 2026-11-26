"""SNMP stays the working if_mib module (plus error and discard counters from the same IF-MIB table)."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_snmp_yml_still_loads_if_mib_only():
    parsed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))
    assert set(parsed["modules"]) == {"if_mib", "host", "power", "routing"}
    module = parsed["modules"]["if_mib"]
    assert "lookups" not in module
    names = {metric["name"] for metric in module["metrics"]}
    assert names == {"sysUpTime", "ifAdminStatus", "ifOperStatus", "ifHCInOctets", "ifHCOutOctets",
                     "ifInErrors", "ifOutErrors", "ifInDiscards", "ifOutDiscards"}
    oper = next(metric for metric in module["metrics"] if metric["name"] == "ifOperStatus")
    labels = {item["labelname"] for item in oper["lookups"]}
    assert labels == {"ifAlias", "ifDescr", "ifName"}


