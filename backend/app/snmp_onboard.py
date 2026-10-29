"""Bounded SNMP GET/GET-NEXT for Discovery → SNMP devices. Not a MIB compiler and not a full walk.

v1/v2c only (stdlib BER). v3 stays on snmp_exporter scrape after ``./forgesre snmp-auths``.
"""

from __future__ import annotations

import os
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.asset_snmp import generated_snmp_path, snmp_settings

ONBOARD_BEGIN = "  # forgesre-onboard-modules begin"
ONBOARD_END = "  # forgesre-onboard-modules end"
ONBOARD_MODULE = "forgesre_onboard"
MAX_OIDS_PER_PASS = 12
DEFAULT_TIMEOUT = 0.8

UdpSend = Callable[[bytes, tuple[str, int], float], bytes | None]


@dataclass(frozen=True)
class OidDef:
    key: str
    name: str
    oid: str
    group: str
    kind: str  # scalar | table
    help: str
    collected: bool = False


# Compact catalog: IF-MIB + a few HOST-RESOURCES / UCD / UPS probes. Not every OID in those MIBs.
CATALOG: tuple[OidDef, ...] = (
    OidDef("sysDescr", "sysDescr", "1.3.6.1.2.1.1.1.0", "system", "scalar", "Device description"),
    OidDef("sysObjectID", "sysObjectID", "1.3.6.1.2.1.1.2.0", "system", "scalar", "Vendor object ID"),
    OidDef("sysUpTime", "sysUpTime", "1.3.6.1.2.1.1.3.0", "system", "scalar", "Time since SNMP agent restart"),
    OidDef("sysName", "sysName", "1.3.6.1.2.1.1.5.0", "system", "scalar", "SNMP sysName"),
    OidDef("ifNumber", "ifNumber", "1.3.6.1.2.1.2.1.0", "interfaces", "scalar", "Interface count (cardinality)"),
    OidDef("ifDescr", "ifDescr", "1.3.6.1.2.1.2.2.1.2", "interfaces", "table", "Interface description"),
    OidDef("ifOperStatus", "ifOperStatus", "1.3.6.1.2.1.2.2.1.8", "interfaces", "table", "Operational state"),
    OidDef("ifHCInOctets", "ifHCInOctets", "1.3.6.1.2.1.31.1.1.1.6", "traffic", "table", "In octets"),
    OidDef("ifHCOutOctets", "ifHCOutOctets", "1.3.6.1.2.1.31.1.1.1.10", "traffic", "table", "Out octets"),
    OidDef("ifInErrors", "ifInErrors", "1.3.6.1.2.1.2.2.1.14", "errors", "table", "In errors"),
    OidDef("hrProcessorLoad", "hrProcessorLoad", "1.3.6.1.2.1.25.3.3.1.2", "cpu", "table", "HOST-RESOURCES CPU load"),
    OidDef("hrStorageUsed", "hrStorageUsed", "1.3.6.1.2.1.25.2.3.1.6", "storage", "table", "HOST-RESOURCES storage used"),
    OidDef("ssCpuIdle", "ssCpuIdle", "1.3.6.1.4.1.2021.11.11.0", "cpu", "scalar", "UCD CPU idle"),
    OidDef("memAvailReal", "memAvailReal", "1.3.6.1.4.1.2021.4.6.0", "memory", "scalar", "UCD available RAM"),
    OidDef("upsOutputSource", "upsOutputSource", "1.3.6.1.2.1.33.1.4.1.0", "ups", "scalar", "UPS-MIB output source"),
    OidDef("entPhySensorValue", "entPhySensorValue", "1.3.6.1.2.1.99.1.1.1.4", "health", "table", "ENTITY-SENSOR value"),
)

IDENTIFY_KEYS = ("sysDescr", "sysObjectID", "sysUpTime", "sysName")
RECOMMEND_KEYS = (
    "ifNumber",
    "ifOperStatus",
    "ifHCInOctets",
    "ifInErrors",
    "hrProcessorLoad",
    "hrStorageUsed",
    "ssCpuIdle",
    "memAvailReal",
    "upsOutputSource",
    "entPhySensorValue",
)


def catalog_by_key() -> dict[str, OidDef]:
    return {item.key: item for item in CATALOG}


def normalize_oid(raw: str) -> str:
    text = "".join(ch for ch in str(raw or "").strip() if ch in "0123456789.")
    parts = [part for part in text.split(".") if part]
    if len(parts) < 2:
        raise ValueError("Need a numeric OID (at least two arcs)")
    nums = [int(part) for part in parts]
    if any(n < 0 or n > 2_147_483_647 for n in nums):
        raise ValueError("OID arc out of range")
    return ".".join(str(n) for n in nums)


def encode_oid(oid: str) -> bytes:
    parts = [int(part) for part in normalize_oid(oid).split(".")]
    head = 40 * parts[0] + parts[1]
    out = bytearray([head])
    for n in parts[2:]:
        stack = [n & 0x7F]
        n >>= 7
        while n:
            stack.append(0x80 | (n & 0x7F))
            n >>= 7
        out.extend(reversed(stack))
    return bytes([0x06, len(out)]) + bytes(out)


def _ber(tag: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 128:
        return bytes([tag, n]) + payload
    if n < 256:
        return bytes([tag, 0x81, n]) + payload
    return bytes([tag, 0x82, n >> 8, n & 0xFF]) + payload


def snmp_get_packet(oid: str, community: str, *, version: int = 1, get_next: bool = False, req_id: int = 1) -> bytes:
    """v1 version byte 0, v2c version byte 1. GET = 0xA0, GET-NEXT = 0xA1."""
    comm = (community or "public").encode("latin-1", "replace")[:32]
    oid_tlv = encode_oid(oid)
    vb = _ber(0x30, oid_tlv + bytes([0x05, 0x00]))
    vbl = _ber(0x30, vb)
    pdu_body = bytes([0x02, 0x01, req_id & 0x7F, 0x02, 0x01, 0x00, 0x02, 0x01, 0x00]) + vbl
    pdu = _ber(0xA1 if get_next else 0xA0, pdu_body)
    ver = bytes([0x02, 0x01, 0 if version == 0 else 1])
    comm_tlv = bytes([0x04, len(comm)]) + comm
    return _ber(0x30, ver + comm_tlv + pdu)


def _read_len(data: bytes, i: int) -> tuple[int, int]:
    if i >= len(data):
        raise ValueError("truncated BER")
    first = data[i]
    if first < 128:
        return first, i + 1
    n = first & 0x7F
    if n == 0 or i + n >= len(data):
        raise ValueError("bad BER length")
    value = 0
    for byte in data[i + 1 : i + 1 + n]:
        value = (value << 8) | byte
    return value, i + 1 + n


def _decode_int(payload: bytes) -> int:
    if not payload:
        return 0
    value = 0
    for byte in payload:
        value = (value << 8) | byte
    if payload[0] & 0x80:
        value -= 1 << (8 * len(payload))
    return value


def _collect_values(data: bytes) -> list[str]:
    values: list[str] = []
    i = 0
    while i < len(data):
        tag = data[i]
        length, i = _read_len(data, i + 1)
        payload = data[i : i + length]
        i += length
        if tag == 0x04:
            text = payload.decode("utf-8", "replace").strip("\x00")
            if text and all(32 <= ord(ch) < 127 or ch in "\t\n" for ch in text):
                values.append(text)
            elif payload:
                values.append("hex:" + payload[:48].hex())
        elif tag in {0x02, 0x41, 0x42, 0x43, 0x46, 0x47}:
            values.append(str(_decode_int(payload)))
        elif tag == 0x06:
            continue
        elif tag in {0x30, 0xA0, 0xA1, 0xA2}:
            values.extend(_collect_values(payload))
        elif tag == 0x05:
            continue
    return values


def parse_snmp_value(packet: bytes) -> str:
    """Best-effort value from a GET/GET-NEXT response. Community string is skipped, never returned."""
    if not packet:
        raise ValueError("empty SNMP reply")
    body = packet
    if packet[0] == 0x30:
        length, i = _read_len(packet, 1)
        body = packet[i : i + length]
        if body and body[0] == 0x02:
            n, j = _read_len(body, 1)
            body = body[j + n :]
        if body and body[0] == 0x04:
            n, j = _read_len(body, 1)
            body = body[j + n :]
    values = _collect_values(body)
    return values[-1] if values else "—"


def _udp_exchange(packet: bytes, addr: tuple[str, int], timeout: float) -> bytes | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, addr)
        data, _from = sock.recvfrom(4096)
        return data or None
    except OSError:
        return None
    finally:
        sock.close()


def snmp_get(
    ip: str,
    oid: str,
    *,
    community: str = "public",
    port: int = 161,
    version: str = "v2c",
    timeout: float = DEFAULT_TIMEOUT,
    get_next: bool = False,
    send: UdpSend | None = None,
) -> dict[str, Any]:
    """One bounded GET. Never walks a tree. ``send`` is for tests."""
    try:
        oid_n = normalize_oid(oid)
    except ValueError as exc:
        return {"ok": False, "oid": oid, "error": str(exc), "value": ""}
    ver = 0 if str(version).lower() in {"v1", "1"} else 1
    packet = snmp_get_packet(oid_n, community or "public", version=ver, get_next=get_next)
    fn = send or _udp_exchange
    raw = fn(packet, (ip, int(port or 161)), float(timeout))
    if not raw:
        return {"ok": False, "oid": oid_n, "error": "no SNMP reply", "value": ""}
    try:
        value = parse_snmp_value(raw)
    except ValueError as exc:
        return {"ok": False, "oid": oid_n, "error": str(exc), "value": ""}
    return {"ok": True, "oid": oid_n, "error": "", "value": value}


def community_for_asset(asset: Any, posted: str = "") -> str:
    typed = (posted or "").strip()
    if typed:
        return typed
    stored = snmp_settings(getattr(asset, "extras", None) if asset is not None else None)
    if stored.get("snmp_community_mode") == "custom" and stored.get("snmp_community"):
        return stored["snmp_community"]
    return os.environ.get("SNMP_COMMUNITY") or "public"


def collected_oids(path: Path | None = None) -> set[str]:
    """OIDs already in committed if_mib + host modules (walk + get + metric oid)."""
    target = path or (Path(__file__).resolve().parents[2] / "monitoring" / "snmp.yml")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return set()
    import yaml

    data = yaml.safe_load(text.replace("__SNMP_COMMUNITY__", "public")) or {}
    modules = data.get("modules") if isinstance(data, dict) else None
    if not isinstance(modules, dict):
        return set()
    out: set[str] = set()
    for module in modules.values():
        if not isinstance(module, dict):
            continue
        for oid in list(module.get("walk") or []) + list(module.get("get") or []):
            try:
                out.add(normalize_oid(str(oid)))
            except ValueError:
                continue
        for metric in module.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            try:
                out.add(normalize_oid(str(metric.get("oid") or "")))
            except ValueError:
                continue
    return out


def clamp_timeout(raw: Any, default: float = DEFAULT_TIMEOUT) -> float:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        n = default
    return max(0.2, min(n, 3.0))


def _probe_def(item: OidDef, ip: str, **kwargs: Any) -> dict[str, Any]:
    result = snmp_get(ip, item.oid, get_next=item.kind == "table", **kwargs)
    if not result["ok"] and item.kind == "table":
        result = snmp_get(ip, item.oid + ".1", get_next=False, **kwargs)
    result["key"] = item.key
    result["name"] = item.name
    result["group"] = item.group
    result["help"] = item.help
    result["kind"] = item.kind
    return result


def identify_device(ip: str, **kwargs: Any) -> dict[str, Any]:
    by = catalog_by_key()
    rows = [_probe_def(by[key], ip, **kwargs) for key in IDENTIFY_KEYS]
    descr = next((row["value"] for row in rows if row["key"] == "sysDescr" and row["ok"]), "")
    name = next((row["value"] for row in rows if row["key"] == "sysName" and row["ok"]), "")
    return {"ok": any(row["ok"] for row in rows), "sysDescr": descr, "sysName": name, "rows": rows}


def recommend_metrics(ip: str, **kwargs: Any) -> list[dict[str, Any]]:
    collected = collected_oids()
    by = catalog_by_key()
    out: list[dict[str, Any]] = []
    for key in RECOMMEND_KEYS:
        item = by[key]
        row = _probe_def(item, ip, **kwargs)
        base = normalize_oid(item.oid)
        in_prom = any(oid == base or oid.startswith(base + ".") or base.startswith(oid + ".") for oid in collected)
        row["in_mib"] = True
        row["in_prometheus"] = in_prom
        row["recommend"] = bool(row["ok"]) and not in_prom
        row["applyable"] = bool(row["recommend"] and item.kind == "scalar")
        out.append(row)
    return out


def cardinality_hint(if_number: str, extra_table_cols: int) -> str:
    try:
        n = int(str(if_number).split()[0])
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "Interface count unknown — avoid a full walk."
    series = n * max(extra_table_cols, 0)
    if n > 200 or series > 800:
        return f"ifNumber={n}. Extra table columns ≈ {series} series. High cardinality — keep the if_mib slice."
    return f"ifNumber={n}. Extra table columns ≈ {series} Prometheus series."


def splice_onboard_module(text: str, block: str) -> str:
    if ONBOARD_BEGIN not in text or ONBOARD_END not in text:
        raise ValueError("snmp.yml is missing forgesre-onboard-modules markers")
    head, rest = text.split(ONBOARD_BEGIN, 1)
    begin_line, _, _old = rest.partition("\n")
    tail = rest.split(ONBOARD_END, 1)[1]
    body = block if not block or block.endswith("\n") else block + "\n"
    return f"{head}{ONBOARD_BEGIN}{begin_line}\n{body}{ONBOARD_END}{tail}"


def onboard_module_yaml(oids: list[str]) -> str:
    """v0.26 module: get-only extra scalars. Table OIDs stay recommendations until a later generator pass."""
    gets: list[str] = []
    metrics: list[str] = []
    seen: set[str] = set()
    for raw in oids:
        try:
            oid = normalize_oid(raw)
        except ValueError:
            continue
        if oid in seen:
            continue
        seen.add(oid)
        inst = oid if oid.endswith(".0") or oid.count(".") > 8 else oid
        gets.append(f"      - {inst}")
        name = "oid_" + oid.replace(".", "_")
        metrics.append(
            "      - name: "
            + name
            + "\n        oid: "
            + (oid[:-2] if oid.endswith(".0") else oid)
            + "\n        type: gauge\n        help: Operator-selected SNMP OID "
            + oid
        )
    if not gets:
        return ""
    return (
        f"  {ONBOARD_MODULE}:\n"
        "    timeout: 5s\n"
        "    retries: 1\n"
        "    max_repetitions: 10\n"
        "    get:\n"
        + "\n".join(gets)
        + "\n    metrics:\n"
        + "\n".join(metrics)
        + "\n"
    )


def apply_onboard_module(oids: list[str], path: Path | None = None) -> dict[str, Any]:
    target = path or generated_snmp_path()
    if not target.is_file():
        return {"ok": False, "error": "Generated snmp.yml is not on this host. Run ./forgesre render-monitoring first."}
    text = target.read_text(encoding="utf-8")
    backup = target.with_suffix(".yml.onboard.bak")
    shutil.copy2(target, backup)
    block = onboard_module_yaml(oids)
    try:
        written = splice_onboard_module(text, block)
        import yaml

        parsed = yaml.safe_load(written.replace("__SNMP_COMMUNITY__", "public"))
        modules = (parsed or {}).get("modules") or {}
        if "if_mib" not in modules:
            raise ValueError("refusing to write snmp.yml without if_mib")
        if block and ONBOARD_MODULE not in modules:
            raise ValueError("onboard module missing after splice")
        target.write_text(written, encoding="utf-8")
    except Exception as exc:
        shutil.copy2(backup, target)
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": True, "error": "", "backup": str(backup), "module": ONBOARD_MODULE if block else ""}


def restore_onboard_module(path: Path | None = None) -> dict[str, Any]:
    target = path or generated_snmp_path()
    backup = target.with_suffix(".yml.onboard.bak")
    if not backup.is_file():
        return {"ok": False, "error": "No onboard backup to restore."}
    shutil.copy2(backup, target)
    return {"ok": True, "error": ""}


def reload_exporter(url: str | None = None) -> bool:
    """POST snmp_exporter /-/reload so a generated onboard module is picked up."""
    base = (url or os.environ.get("SNMP_EXPORTER_URL") or "http://127.0.0.1:9116").rstrip("/")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/-/reload", method="POST"), timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def exporter_scrape(
    target: str,
    *,
    module: str = "if_mib",
    auth: str = "public_v2",
    timeout: float = 3.0,
    url: str | None = None,
) -> dict[str, Any]:
    """One snmp_exporter scrape (v3 identify / Test scrape). No community string in the URL."""
    host = (target or "").strip()
    if not host:
        return {"ok": False, "error": "Need a target IP", "body": ""}
    base = (url or os.environ.get("SNMP_EXPORTER_URL") or "http://127.0.0.1:9116").rstrip("/")
    query = urllib.parse.urlencode({"target": host, "module": module, "auth": auth})
    req = urllib.request.Request(f"{base}/snmp?{query}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")[:8000]
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:200], "body": ""}
    ok = any(token in body for token in ("sysUpTime", "ifOperStatus", "snmp_", "oid_1_3_6"))
    return {
        "ok": ok,
        "error": "" if ok else "no SNMP metrics in scrape",
        "body": body[:1500],
        "module": module,
        "auth": auth,
        "target": host,
    }
