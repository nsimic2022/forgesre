"""Lightweight CIDR probe. Not a replacement for nmap/Netdisco."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from typing import Any

LINUX_PORTS = {22, 9100}
WINDOWS_PORTS = {9182}
WEB_PORTS = {80, 443}
SNMP_PORTS = {161}
DEFAULT_PORTS = (22, 80, 443, 161, 9100, 9182)
MAX_HOSTS_PER_CIDR = 256
MAX_HOSTS_TOTAL = 1024

_IP_ADDR_RE = re.compile(
    r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)\b",
    re.MULTILINE,
)


def is_docker_bridge(ifname: str) -> bool:
    """True for docker0, br-*, and veth* — skipped by default on Scan now."""
    name = (ifname or "").strip().lower()
    return name == "docker0" or name.startswith("br-") or name.startswith("veth")


def _skip_network(network: ipaddress.IPv4Network) -> bool:
    if network.version != 4:
        return True
    if network.prefixlen == 0 or network == ipaddress.ip_network("0.0.0.0/0"):
        return True
    if network.is_loopback or network.is_multicast or network.is_unspecified:
        return True
    if network.is_link_local:
        return True
    return False


def _skip_host(host: ipaddress.IPv4Address) -> bool:
    return bool(
        host.is_loopback
        or host.is_multicast
        or host.is_unspecified
        or host.is_link_local
    )


def _discovery_auto_enabled() -> bool:
    """Tests set FORGESRE_DISCOVERY_AUTO=0 so pytest never probes the live LAN."""
    raw = os.environ.get("FORGESRE_DISCOVERY_AUTO", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _ip_cmd_output() -> str:
    """`ip -4 -o addr show` from iproute2 (Core image). Tries common paths."""
    for binary in ("ip", "/sbin/ip", "/usr/sbin/ip", "/bin/ip"):
        try:
            proc = subprocess.run(
                [binary, "-4", "-o", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.stdout:
                return proc.stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def _parse_ip_addr_output(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in _IP_ADDR_RE.finditer(text or ""):
        iface, addr_s, prefix_s = match.group(1), match.group(2), match.group(3)
        iface = iface.split("@", 1)[0]
        try:
            addr = ipaddress.ip_address(addr_s)
            prefixlen = int(prefix_s)
        except ValueError:
            continue
        if addr.version != 4 or prefixlen < 0 or prefixlen > 32:
            continue
        rows.append({"iface": iface, "addr": str(addr), "prefixlen": prefixlen})
    return rows


def _ioctl_inet_rows() -> list[dict[str, Any]]:
    """Linux SIOCGIFADDR / SIOCGIFNETMASK — real prefixlen if `ip` is missing."""
    try:
        import fcntl
        import struct
    except ImportError:
        return []
    try:
        names = [name for _idx, name in socket.if_nameindex()]
    except OSError:
        return []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rows: list[dict[str, Any]] = []
    try:
        for name in names:
            packed = name.encode("ascii", "replace")[:15]
            ifreq = struct.pack("256s", packed)
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(sock, 0x8915, ifreq)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(sock, 0x891B, ifreq)[20:24])
                prefixlen = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            except OSError:
                continue
            rows.append({"iface": name.split("@", 1)[0], "addr": addr, "prefixlen": prefixlen})
    finally:
        sock.close()
    return rows


def _iface_inet_rows(ip_output: str | None = None) -> list[dict[str, Any]]:
    """Parse `ip -4 -o addr show` rows into iface/addr/prefixlen dicts."""
    if ip_output is not None:
        return _parse_ip_addr_output(ip_output)
    rows = _parse_ip_addr_output(_ip_cmd_output())
    if rows:
        return rows
    return _ioctl_inet_rows()


def detect_connected_networks(
    *,
    include_docker: bool = False,
    ip_output: str | None = None,
    limit: int = MAX_HOSTS_PER_CIDR,
    total_limit: int = MAX_HOSTS_TOTAL,
) -> dict[str, Any]:
    """Enumerate host IPv4 connected networks (real prefixlen).

    Core runs with ``network_mode: host``, so this sees appliance interfaces.
    Skips loopback, link-local, multicast, ``0.0.0.0/0``, and by default Docker
    bridges (``docker0``, ``br-*``, ``veth*``). Returns actual prefixes — never
    hardcodes ``/24``.
    """
    if ip_output is None and not _discovery_auto_enabled():
        return {
            "cidrs": [],
            "interfaces": [],
            "skipped": [],
            "warnings": [],
            "host_count": 0,
            "truncated": False,
        }
    interfaces: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    cidrs: list[str] = []
    seen: set[str] = set()
    for row in _iface_inet_rows(ip_output):
        iface = str(row["iface"])
        if not include_docker and is_docker_bridge(iface):
            skipped.append({"iface": iface, "reason": "docker_bridge"})
            continue
        try:
            iface_ip = ipaddress.ip_interface(f"{row['addr']}/{row['prefixlen']}")
        except ValueError:
            skipped.append({"iface": iface, "reason": "invalid"})
            continue
        network = iface_ip.network
        if _skip_network(network):
            reason = "loopback" if network.is_loopback else (
                "link_local" if network.is_link_local else (
                    "multicast" if network.is_multicast else (
                        "default_route" if network.prefixlen == 0 else "excluded"
                    )
                )
            )
            skipped.append({"iface": iface, "reason": reason})
            continue
        cidr = str(network)
        interfaces.append(
            {
                "iface": iface,
                "addr": str(iface_ip.ip),
                "cidr": cidr,
                "prefixlen": int(network.prefixlen),
            }
        )
        if cidr not in seen:
            seen.add(cidr)
            cidrs.append(cidr)
    plan = scan_plan(cidrs, limit=limit, total_limit=total_limit)
    return {
        "cidrs": cidrs,
        "interfaces": interfaces,
        "skipped": skipped,
        "warnings": list(plan["warnings"]),
        "host_count": int(plan["total"]),
        "truncated": bool(plan["truncated"]),
    }


def suggested_connected_cidrs(
    *,
    include_docker: bool = False,
    ip_output: str | None = None,
) -> list[str]:
    """Connected IPv4 CIDRs with real prefixes (multi-homed OK). Not /24-only."""
    return list(
        detect_connected_networks(include_docker=include_docker, ip_output=ip_output)["cidrs"]
    )


def normalize_cidrs(raw: list[str] | str | None) -> list[str]:
    """Parse operator CIDR text into unique IPv4 networks. Invalid / excluded dropped."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.replace("\n", ",").replace(";", ",").split(",")]
    else:
        parts = [str(item).strip() for item in raw]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        try:
            network = ipaddress.ip_network(part, strict=False)
        except ValueError:
            continue
        if network.version != 4 or _skip_network(network):
            continue
        text = str(network)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out



def merge_cidrs(*groups: list[str] | None) -> list[str]:
    """Dedupe IPv4 CIDRs preserving first-seen order across groups."""
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for cidr in normalize_cidrs(group):
            if cidr in seen:
                continue
            seen.add(cidr)
            out.append(cidr)
    return out


def resolve_scan_cidrs(
    yaml_cidrs: list[str] | str | None = None,
    *,
    include_docker: bool = False,
    ip_output: str | None = None,
    limit: int = MAX_HOSTS_PER_CIDR,
    total_limit: int = MAX_HOSTS_TOTAL,
) -> dict[str, Any]:
    """Union of live YAML ``discovery.cidrs`` and auto-detected connected nets.

    Never hardcodes ``/24``.
    """
    from_yaml = normalize_cidrs(yaml_cidrs)
    detected = detect_connected_networks(
        include_docker=include_docker,
        ip_output=ip_output,
        limit=limit,
        total_limit=total_limit,
    )
    from_auto = list(detected["cidrs"])
    merged = merge_cidrs(from_yaml, from_auto)
    if from_yaml and from_auto:
        source = "yaml+auto"
    elif from_yaml:
        source = "yaml"
    elif from_auto:
        source = "auto"
    else:
        source = "none"
    plan = scan_plan(merged, limit=limit, total_limit=total_limit)
    return {
        "cidrs": merged,
        "yaml": from_yaml,
        "auto": from_auto,
        "source": source,
        "interfaces": list(detected["interfaces"]),
        "skipped": list(detected["skipped"]),
        "warnings": list(plan["warnings"]),
        "host_count": int(plan["total"]),
        "truncated": bool(plan["truncated"]),
        "per_cidr": list(plan["per_cidr"]),
        "hosts": list(plan["hosts"]),
    }



def scan_plan(
    cidrs: list[str],
    limit: int = MAX_HOSTS_PER_CIDR,
    total_limit: int = MAX_HOSTS_TOTAL,
) -> dict[str, Any]:
    """Expand CIDRs with per-network truncation warnings (256/CIDR, 1024 total)."""
    hosts: list[str] = []
    per_cidr: list[dict[str, Any]] = []
    warnings: list[str] = []
    truncated = False
    for raw in cidrs:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        if network.version != 4 or _skip_network(network):
            continue
        usable = 0
        for host in network.hosts() if network.num_addresses > 2 else network:
            if _skip_host(host):
                continue
            usable += 1
        room = max(0, total_limit - len(hosts))
        take = min(limit, room, usable)
        cidr_hosts: list[str] = []
        for host in network.hosts() if network.num_addresses > 2 else network:
            if _skip_host(host):
                continue
            cidr_hosts.append(str(host))
            if len(cidr_hosts) >= take:
                break
        hosts.extend(cidr_hosts)
        was_cut = usable > take
        if was_cut:
            truncated = True
            if usable > limit:
                warnings.append(
                    f"{network} has {usable} usable hosts; scan probes the first {take} "
                    f"(limit {limit}/CIDR)."
                )
            elif room < usable:
                warnings.append(
                    f"{network}: total host budget {total_limit} reached; "
                    f"only {take} of {usable} hosts queued."
                )
        per_cidr.append(
            {
                "cidr": str(network),
                "usable": usable,
                "planned": len(cidr_hosts),
                "truncated": was_cut,
            }
        )
        if len(hosts) >= total_limit:
            truncated = True
            if not any("total host budget" in w for w in warnings):
                warnings.append(
                    f"Total host budget is {total_limit}; later CIDRs are truncated or skipped."
                )
            break
    return {
        "hosts": hosts,
        "per_cidr": per_cidr,
        "warnings": warnings,
        "total": len(hosts),
        "truncated": truncated,
    }


def hosts_from_cidrs(
    cidrs: list[str],
    limit: int = MAX_HOSTS_PER_CIDR,
    total_limit: int = MAX_HOSTS_TOTAL,
) -> list[str]:
    """Enumerate hosts. `limit` is per CIDR (default 256). Truncation via scan_plan."""
    return list(scan_plan(cidrs, limit=limit, total_limit=total_limit)["hosts"])


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
    alive = bool(open_ports) or snmp_ok
    exporter_kind = ""
    detect_message = ""
    tcp_ports = [p for p in open_ports if p != 161]
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


def probe_snmp_udp(ip: str, community: str | None = None, timeout: float = 0.4) -> bool:
    """True if UDP/161 answers an SNMPv2c GET sysDescr. TCP/161 is not SNMP."""
    community = community or os.environ.get("SNMP_COMMUNITY") or "public"
    packet = snmp_get_sysdescr_packet(community)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, 161))
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
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False
