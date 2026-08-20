"""Lightweight CIDR probe. Not a replacement for nmap/Netdisco."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

LINUX_PORTS = {22, 9100}
WEB_PORTS = {80, 443}
SNMP_PORTS = {161}
DEFAULT_PORTS = (22, 80, 443, 161, 9100)
MAX_HOSTS = 256


def hosts_from_cidrs(cidrs: list[str], limit: int = MAX_HOSTS) -> list[str]:
    hosts: list[str] = []
    for raw in cidrs:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        iterator = network.hosts() if network.num_addresses > 2 else network
        for host in iterator:
            if host.is_loopback or host.is_multicast or host.is_unspecified:
                continue
            hosts.append(str(host))
            if len(hosts) >= limit:
                return hosts
    return hosts


def probe_host(ip: str, ports: tuple[int, ...] = DEFAULT_PORTS, timeout: float = 0.2) -> dict[str, Any]:
    open_ports: list[int] = []
    for port in ports:
        if _open(ip, port, timeout):
            open_ports.append(port)
    return {
        "ip": ip,
        "open_ports": open_ports,
        "proposed_role": classify(open_ports),
        "alive": bool(open_ports),
    }


def classify(open_ports: list[int]) -> str:
    ports = set(open_ports)
    if ports & LINUX_PORTS:
        return "Possible Linux server"
    if ports & SNMP_PORTS:
        return "Possible network device"
    if ports & WEB_PORTS:
        return "Possible web/appliance"
    if ports:
        return "Unknown device with open ports"
    return "No open ports"


def _open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False
