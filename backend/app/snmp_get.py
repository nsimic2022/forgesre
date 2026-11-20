"""Tiny SNMP v1/v2c GET for identifying a device before it is added: sysDescr, sysObjectID,
sysName, sysLocation. Stdlib only (UDP + BER), one packet, short timeout.

Not a poller: snmp_exporter walks devices once they are in the inventory. SNMPv3 is not done here;
v3 credentials are proved through snmp_exporter itself after the device is added.
"""

from __future__ import annotations

import socket
from typing import Any

SYSTEM_OIDS = {
    "1.3.6.1.2.1.1.1.0": "sys_descr",
    "1.3.6.1.2.1.1.2.0": "sys_object_id",
    "1.3.6.1.2.1.1.5.0": "sys_name",
    "1.3.6.1.2.1.1.6.0": "sys_location",
}


def _ber(tag: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 128:
        return bytes([tag, n]) + payload
    if n < 256:
        return bytes([tag, 0x81, n]) + payload
    return bytes([tag, 0x82, n >> 8, n & 0xFF]) + payload


def _oid(text: str) -> bytes:
    parts = [int(p) for p in text.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for n in parts[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.insert(0, 0x80 | (n & 0x7F))
            n >>= 7
        out.extend(chunk)
    return _ber(0x06, bytes(out))


def get_packet(community: str, oids: list[str], version: int = 1, request_id: int = 1) -> bytes:
    """GetRequest for ``oids``. ``version`` is the wire value: 0 = v1, 1 = v2c."""
    varbinds = b"".join(_ber(0x30, _oid(o) + b"\x05\x00") for o in oids)
    pdu = _ber(0xA0, _ber(0x02, request_id.to_bytes(4, "big").lstrip(b"\x00") or b"\x00") + b"\x02\x01\x00\x02\x01\x00" + _ber(0x30, varbinds))
    comm = community.encode("utf-8", "replace")[:64]
    return _ber(0x30, bytes([0x02, 0x01, version]) + _ber(0x04, comm) + pdu)


def _read(data: bytes, i: int) -> tuple[int, bytes, int]:
    tag = data[i]
    length = data[i + 1]
    i += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[i:i + count], "big")
        i += count
    return tag, data[i:i + length], i + length


def _oid_text(raw: bytes) -> str:
    if not raw:
        return ""
    parts = [raw[0] // 40, raw[0] % 40]
    n = 0
    for byte in raw[1:]:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(n)
            n = 0
    return ".".join(str(p) for p in parts)


def parse_response(data: bytes) -> dict[str, Any]:
    """{oid: value} from a GetResponse. Values: text, OID text or int; errors raise ValueError."""
    _tag, message, _ = _read(data, 0)
    i = 0
    _t, _version, i = _read(message, i)
    _t, _community, i = _read(message, i)
    tag, pdu, _ = _read(message, i)
    if tag != 0xA2:
        raise ValueError("not an SNMP response")
    j = 0
    _t, _rid, j = _read(pdu, j)
    _t, status, j = _read(pdu, j)
    if int.from_bytes(status, "big"):
        raise ValueError(f"SNMP error status {int.from_bytes(status, 'big')}")
    _t, _index, j = _read(pdu, j)
    _t, varbinds, _ = _read(pdu, j)
    out: dict[str, Any] = {}
    k = 0
    while k < len(varbinds):
        _t, vb, k = _read(varbinds, k)
        _t, oid_raw, m = _read(vb, 0)
        vtag, value, _ = _read(vb, m)
        oid = _oid_text(oid_raw)
        if vtag == 0x04:
            out[oid] = value.decode("utf-8", "replace").strip()
        elif vtag == 0x06:
            out[oid] = _oid_text(value)
        elif vtag in (0x02, 0x41, 0x42, 0x43):
            out[oid] = int.from_bytes(value, "big")
        else:
            out[oid] = None  # noSuchObject / noSuchInstance / endOfMibView
    return out


def system_info(ip: str, community: str, *, version: str = "v2c", port: int = 161, timeout: float = 1.5) -> dict[str, Any]:
    """sysDescr / sysObjectID / sysName / sysLocation, or {"error": why}. Never raises."""
    packet = get_packet(community, list(SYSTEM_OIDS), version=0 if version == "v1" else 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, int(port or 161)))
        data, _addr = sock.recvfrom(65535)
        values = parse_response(data)
    except socket.timeout:
        return {"error": f"no SNMP answer from {ip}:{port} in {timeout:g} s (wrong community, version or ACL?)"}
    except (OSError, ValueError, IndexError) as exc:
        return {"error": f"SNMP: {exc}"}
    finally:
        sock.close()
    return {name: values.get(oid) or "" for oid, name in SYSTEM_OIDS.items()}


VENDORS = (
    ("cisco", "Cisco"), ("juniper", "Juniper"), ("junos", "Juniper"), ("mikrotik", "MikroTik"), ("routeros", "MikroTik"),
    ("aruba", "Aruba"), ("procurve", "HPE"), ("hewlett", "HPE"), ("fortigate", "Fortinet"), ("fortinet", "Fortinet"),
    ("palo alto", "Palo Alto"), ("ubiquiti", "Ubiquiti"), ("edgeos", "Ubiquiti"), ("huawei", "Huawei"), ("dell", "Dell"),
    ("netgear", "Netgear"), ("synology", "Synology"), ("qnap", "QNAP"), ("vmware", "VMware"), ("esxi", "VMware"),
    ("windows", "Microsoft"), ("linux", "Linux"), ("apc", "APC"), ("printer", "Printer"),
)


def guess_type(sys_descr: str) -> tuple[str, str]:
    """(vendor, asset type) from sysDescr, e.g. ("Cisco", "Switch"). Empty strings when unknown."""
    text = (sys_descr or "").lower()
    vendor = next((name for key, name in VENDORS if key in text), "")
    if any(k in text for k in ("fortigate", "palo alto", "asa ", "firewall", "pfsense", "opnsense")):
        return vendor, "Firewall"
    if any(k in text for k in ("router", "routeros", "isr", "asr", "junos", "edgeos")):
        return vendor, "Router"
    if any(k in text for k in ("switch", "catalyst", "nexus", "procurve", "aruba", "ex2", "ex3", "ex4")):
        return vendor, "Switch"
    if any(k in text for k in ("synology", "qnap", "diskstation")):
        return vendor, "QNAP/NAS"
    if "esxi" in text or "vmware" in text:
        return vendor, "Hypervisor"
    if "printer" in text or "jetdirect" in text:
        return vendor, "Printer"
    if "windows" in text:
        return vendor, "Windows Server"
    if "linux" in text:
        return vendor, "Linux Server"
    return vendor, "Network device" if text else ""
