"""IBM Flash is a Storage SNMP asset. The walk is if_mib only — no vendor MIB."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.asset_types import snmp_family, snmp_port_for
from app.inventory import is_snmp_asset
from app.models import Asset
from rca.collector import promql_queries_for

ROOT = Path(__file__).resolve().parents[1]


def test_storage_type_is_snmp_family_on_udp_161():
    assert snmp_family("Storage") is True
    assert snmp_family("QNAP/NAS") is True
    assert snmp_port_for(Asset(type="Storage", ip="10.8.8.8")) == 161
    row = Asset(asset_id="flash-01", hostname="flash-01", type="Storage", ip="10.8.8.8")
    assert is_snmp_asset(row) is True


def test_snmp_walk_is_if_mib_only_no_ibm_oids():
    parsed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))
    assert set(parsed["modules"]) == {"if_mib"}
    names = {metric["name"] for metric in parsed["modules"]["if_mib"]["metrics"]}
    assert names == {"sysUpTime", "ifAdminStatus", "ifOperStatus", "ifHCInOctets", "ifHCOutOctets"}
    walk = "\n".join(str(item) for item in parsed["modules"]["if_mib"].get("walk") or [])
    blob = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8").lower()
    assert "ifHighSpeed" not in names
    assert "1.3.6.1.2.1.31.1.1.1.15" not in walk
    assert "ibm" not in blob and "flashsystem" not in blob and "storwize" not in blob


def test_storage_graphs_are_if_mib_not_linux_and_speed_is_uncollected():
    packed = promql_queries_for({"asset_id": "flash-01", "type": "Storage", "ip": "10.8.8.8"})
    assert "cpu_percent" not in packed
    assert "disk_percent" not in packed
    assert "ifHCInOctets" in packed["net_rx"][0]
    assert "ifHCOutOctets" in packed["net_tx"][0]
    assert 'job="forgesre-snmp"' in packed["up"][0]
    assert "ifHighSpeed" in packed["net_speed"][0]
    snmp = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8")
    assert "ifHighSpeed" not in snmp


def test_forge_does_not_define_storage_volume_alert():
    alerts = (ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8")
    assert "StorageVolumeUsageHigh" not in alerts
    catalog = (ROOT / "agents" / "rca" / "catalog.py").read_text(encoding="utf-8")
    assert "StorageVolumeUsageHigh" in catalog


def test_docs_and_asset_page_say_if_mib_only():
    page = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    assert "module if_mib" in page
    assert "No extra vendor MIB was added." in page
    report = (ROOT / "docs" / "ibm-flash-snmp.md").read_text(encoding="utf-8")
    assert "if_mib" in report
    assert "No extra vendor MIB" in report or "no IBM MIB" in report.lower()
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "ibm-flash-snmp.md" in index
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "ibm-flash-snmp.md" in handbook
