"""Asset Type dropdown and the SNMP family.

Type picks the default HTTP scrape only for Linux Server (:9100) and Windows Server (:9182).
Every other type starts with an empty scrape address (agentless is fine) unless the operator
types one. SNMP is on for the network family (default UDP/161) or when the operator sets an
explicit per-asset ``snmp_port``.
"""

from __future__ import annotations

import re
from typing import Any

from app.exporter_detect import AUTO_ASSET_TYPE, is_auto_asset_type

DEFAULT_SNMP_PORT = 161

ASSET_TYPE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Detect", (AUTO_ASSET_TYPE,)),
    ("Servers", ("Linux Server", "Windows Server", "Hypervisor")),
    ("Network", ("Network device", "Switch", "Router", "Firewall")),
    ("Storage", ("Storage", "QNAP/NAS")),
    ("Other", ("Web/appliance", "Printer", "Other")),
)
ASSET_TYPE_CHOICES: list[str] = [name for _group, names in ASSET_TYPE_GROUPS for name in names]
# Operator picked these on purpose: Verify never rewrites them to Linux / Windows.
PINNED_TYPES = frozenset({"Hypervisor", "Storage", "QNAP/NAS", "Printer", "Other"})

_EXPORTER_WORDS = ("windows", "win32", "linux")
_SNMP_WORDS = re.compile(r"network|switch|router|firewall|storage|qnap|printer|\bnas\b|\bsan\b")


def snmp_family(type: str = "", profile: str = "") -> bool:
    """Network device, Switch, Router, Firewall, Storage, QNAP/NAS, Printer. Not Linux / Windows / Web."""
    if is_auto_asset_type(type):
        return False
    name = (type or "").strip()
    # A listed type decides alone (Hypervisor stays off SNMP even with an old network-switch profile).
    # A saved custom type keeps the old rule: type + profile words.
    blob = name.lower() if name in ASSET_TYPE_CHOICES else f"{name} {profile}".lower()
    if any(word in blob for word in _EXPORTER_WORDS):
        return False
    if "web" in blob or "appliance" in blob:
        return False
    return bool(_SNMP_WORDS.search(blob))


def parse_snmp_port(value: Any) -> int:
    """1–65535, else 0 (not set). Junk and blanks are 0."""
    try:
        port = int(str(value if value is not None else "").strip())
    except ValueError:
        return 0
    return port if 0 < port < 65536 else 0


def snmp_port_for(asset: Any) -> int:
    """Effective SNMP UDP port: explicit ``snmp_port``, else 161 for the network family, else 0 (no SNMP)."""
    explicit = parse_snmp_port(getattr(asset, "snmp_port", None) if not isinstance(asset, dict) else asset.get("snmp_port"))
    if explicit:
        return explicit
    if isinstance(asset, dict):
        type_, profile = str(asset.get("type") or ""), str(asset.get("monitoring_profile") or "")
    else:
        type_, profile = str(getattr(asset, "type", "") or ""), str(getattr(asset, "monitoring_profile", "") or "")
    return DEFAULT_SNMP_PORT if snmp_family(type_, profile) else 0


def snmp_target(ip: str, port: int) -> str:
    """snmp_exporter ``target``: bare IP on 161, ``ip:port`` otherwise."""
    ip = (ip or "").strip()
    if not ip or not port:
        return ""
    return ip if port == DEFAULT_SNMP_PORT else f"{ip}:{port}"
