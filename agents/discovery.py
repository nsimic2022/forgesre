"""Probe one address an operator typed (Add devices › One device, monitoring check).

ForgeSRE does not sweep networks: discovery comes from LibreNMS (app.librenms_discovery).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

log = logging.getLogger("forgesre.discovery")

LINUX_PORTS = {22, 9100}
WINDOWS_PORTS = {9182}
WEB_PORTS = {80, 443}
SNMP_PORTS = {161}
DEFAULT_PORTS = (22, 80, 443, 161, 9100, 9182)
ZABBIX_AGENT_PORT = 10050
ALLOY_HTTP_PORT = 12345


def probe_host(
    ip: str,
    ports: tuple[int, ...] = DEFAULT_PORTS,
    timeout: float = 0.2,
    metrics_fetcher=None,
) -> dict[str, Any]:
    open_ports: list[int] = []
    for port in ports:
        if port == 161:
            continue
        if _open_tcp(ip, port, timeout):
            open_ports.append(port)
    snmp_ok = probe_snmp_udp(ip, timeout=max(timeout, 0.4))
    if snmp_ok:
        open_ports.append(161)
    extra_tcp = [ZABBIX_AGENT_PORT, ALLOY_HTTP_PORT]
    for extra in extra_tcp:
        if extra not in ports and extra not in open_ports and _open_tcp(ip, extra, timeout):
            open_ports.append(extra)
    alive = bool(open_ports) or snmp_ok
    exporter_kind = ""
    detect_message = ""
    tcp_ports = [p for p in open_ports if p not in {161, ZABBIX_AGENT_PORT, ALLOY_HTTP_PORT}]
    if alive and tcp_ports:
        try:
            from app.exporter_detect import detect_exporter
        except ImportError:
            detect_exporter = None  # type: ignore[assignment]
        if detect_exporter is not None:
            detected = detect_exporter(
                ip,
                timeout=max(timeout, 0.8),
                fetcher=metrics_fetcher,
                snmp_ok=snmp_ok,
            )
            exporter_kind = detected.kind or "none"
            detect_message = detected.message
            if detected.kind == "windows" and 9182 not in open_ports:
                open_ports.append(9182)
            if detected.kind == "linux" and 9100 not in open_ports:
                open_ports.append(9100)
    elif snmp_ok:
        exporter_kind = "network"
        detect_message = (
            "SNMP UDP/161 answered. Network device (snmp_exporter path). "
            "Not guessed from HTTP /metrics."
        )
    return {
        "ip": ip,
        "open_ports": open_ports,
        "snmp_ok": snmp_ok,
        "proposed_role": classify(open_ports, snmp_ok=snmp_ok, exporter_kind=exporter_kind),
        "alive": alive,
        "exporter_kind": "" if exporter_kind == "none" else exporter_kind,
        "detect_message": detect_message,
    }


def classify(open_ports: list[int], snmp_ok: bool = False, exporter_kind: str = "") -> str:
    """HTTP exporter family wins. TCP 9100/9182 without /metrics is not an OS pick.

    TCP-only (no exporter_kind): 9100 → Linux, 9182 → Windows. SNMP UDP/161 → network
    (even if SSH is open). SSH-only → Linux without scrape.
    """
    if exporter_kind == "windows":
        return "Possible Windows server"
    if exporter_kind == "linux":
        return "Possible Linux server"
    if exporter_kind == "network":
        return "Possible network device"
    ports = set(open_ports)
    if exporter_kind == "none":
        if snmp_ok or 161 in ports:
            return "Possible network device"
        if 9100 in ports and 9182 in ports:
            return "TCP 9100 and 9182 open (no node_exporter or windows_exporter /metrics — pick OS)"
        if 9182 in ports:
            return "Possible Windows server (TCP 9182, no windows_exporter /metrics)"
        if 9100 in ports:
            return "Possible Linux server (TCP 9100, no node_exporter /metrics)"
        if 22 in ports:
            return "Possible Linux server"
        if ports & WEB_PORTS:
            return "Possible web/appliance"
        if ports:
            return "Unknown device with open ports"
        return "No open ports"
    if 9100 in ports:
        return "Possible Linux server"
    if 9182 in ports:
        return "Possible Windows server"
    if snmp_ok or 161 in ports:
        return "Possible network device"
    if 22 in ports:
        return "Possible Linux server"
    if ports & WEB_PORTS:
        return "Possible web/appliance"
    if ports:
        return "Unknown device with open ports"
    return "No open ports"


def probe_snmp_udp(ip: str, community: str | None = None, timeout: float = 0.4, port: int = 161) -> bool:
    """True if UDP/<port> (default 161) answers an SNMPv2c GET sysDescr. TCP/161 is not SNMP."""
    community = community or os.environ.get("SNMP_COMMUNITY") or "public"
    packet = snmp_get_sysdescr_packet(community)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, int(port or 161)))
        data, _addr = sock.recvfrom(2048)
        return bool(data)
    except OSError:
        return False
    finally:
        sock.close()


def snmp_get_sysdescr_packet(community: str) -> bytes:
    comm = community.encode("latin-1", "replace")[:32]
    oid = bytes([0x06, 0x08, 0x2B, 0x06, 0x01, 0x02, 0x01, 0x01, 0x01, 0x00])
    null = bytes([0x05, 0x00])
    vb = _ber(0x30, oid + null)
    vbl = _ber(0x30, vb)
    pdu_body = bytes([0x02, 0x01, 0x01, 0x02, 0x01, 0x00, 0x02, 0x01, 0x00]) + vbl
    pdu = _ber(0xA0, pdu_body)
    version = bytes([0x02, 0x01, 0x01])
    comm_tlv = bytes([0x04, len(comm)]) + comm
    return _ber(0x30, version + comm_tlv + pdu)


def _ber(tag: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 128:
        return bytes([tag, n]) + payload
    return bytes([tag, 0x81, n]) + payload if n < 256 else bytes([tag, 0x82, n >> 8, n & 0xFF]) + payload


def _open_tcp(ip: str, port: int, timeout: float) -> bool:
    try:
        from app.printer_guard import blocked
    except ImportError:  # agents used alone: never touch 9100 without the guard
        if int(port) == 9100:
            return False
    else:
        if blocked(ip, port):
            return False  # possibly a printer's raw print port
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False
