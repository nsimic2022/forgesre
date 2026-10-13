"""SNMP stays the working if_mib module. Display uses those series; no new MIB was added."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.dc_noc import snmp_surface
from app.models import Asset

ROOT = Path(__file__).resolve().parents[1]


def test_snmp_yml_still_loads_if_mib_only():
    parsed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))
    assert set(parsed["modules"]) == {"if_mib"}
    module = parsed["modules"]["if_mib"]
    assert "lookups" not in module
    names = {metric["name"] for metric in module["metrics"]}
    assert names == {"sysUpTime", "ifAdminStatus", "ifOperStatus", "ifHCInOctets", "ifHCOutOctets"}
    oper = next(metric for metric in module["metrics"] if metric["name"] == "ifOperStatus")
    labels = {item["labelname"] for item in oper["lookups"]}
    assert labels == {"ifAlias", "ifDescr", "ifName"}


def test_if_mib_series_render_ifname_and_uptime_without_a_new_module():
    asset = Asset(asset_id="ups-1", hostname="ups-1", type="Switch", ip="10.9.9.9")

    def vector(expr: str) -> dict:
        if expr.startswith("sysUpTime"):
            return {"series": [{"labels": {}, "value": 100 * 86400 * 2}]}
        return {
            "series": [
                {"labels": {"ifName": "eth0", "ifIndex": "1", "ifAlias": "uplink"}, "value": 1},
            ]
        }

    view = snmp_surface(asset, vector_fn=vector)
    assert view["interfaces"][0]["ifName"] == "eth0"
    assert view["interfaces"][0]["ifAlias"] == "uplink"
    assert view["uptime"] == "2d 0h"
    text = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    assert "No extra vendor MIB was added." in text
    assert "module if_mib" in text
