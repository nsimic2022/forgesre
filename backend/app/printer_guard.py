"""Never send anything to a printer's raw print port.

TCP 9100 is node_exporter on a Linux server, but on a network printer it is the raw print port
(JetDirect): whatever arrives is printed. An HTTP ``GET /metrics`` there prints a page, again on
every scan. So every TCP connect or HTTP request that ForgeSRE makes to port 9100 goes through
``blocked(host, port)`` first, and only goes ahead when the host is known not to be a printer:

1. The inventory decides when it knows the address: a printer type is never touched; a Linux
   server type (the operator said so) is allowed.
2. Otherwise SNMP is asked first (sysDescr, sysObjectID, HOST-RESOURCES hrDeviceType, Printer-MIB):
   a printer answer blocks, any other answer allows.
3. No SNMP answer: an open IPP (631) or LPD (515) port means printer and blocks; an open SSH (22)
   port means a server and allows; nothing at all blocks, with a reason the operator can act on
   (set the type to Linux Server to scrape it).

Loopback addresses are always allowed. Verdicts are kept 10 minutes per process; that is only a
cache, every process can work it out again.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from typing import Any

RAW_PRINT_PORT = 9100
TTL_SECONDS = 600
PRINTER_WORDS = (
    "printer", "jetdirect", "laserjet", "officejet", "designjet", "pagewide", "mfp", "multifunction",
    "xerox", "kyocera", "ricoh", "lexmark", "brother", "konica", "bizhub", "imagerunner", "canon ir",
    "sharp mx", "oki data", "okidata", "epson", "zebra", "workcentre", "versalink", "altalink", "ecosys",
)
HR_DEVICE_TYPE = "1.3.6.1.2.1.25.3.2.1.2.1"
HR_DEVICE_PRINTER = "1.3.6.1.2.1.25.3.1.5"
PRT_GENERAL_NAME = "1.3.6.1.2.1.43.5.1.1.16.1"
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
PRINT_PORTS = (631, 515)

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _host(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text


def _loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def looks_like_printer(text: str) -> bool:
    low = (text or "").lower()
    return any(word in low for word in PRINTER_WORDS)


def snmp_kind(ip: str, community: str = "", *, timeout: float = 1.0) -> str:
    """'printer', 'other' (answered, not a printer) or '' (no answer). One v2c GET, never raises."""
    from app.snmp_get import get_packet, parse_response

    community = community or os.environ.get("SNMP_COMMUNITY") or "public"
    packet = get_packet(community, [SYS_DESCR, SYS_OBJECT_ID, HR_DEVICE_TYPE, PRT_GENERAL_NAME], version=1)
    sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, 161))
        data, _addr = sock.recvfrom(65535)
        values = parse_response(data)
    except (OSError, ValueError, IndexError):
        return ""
    finally:
        sock.close()
    if values.get(HR_DEVICE_TYPE) == HR_DEVICE_PRINTER or values.get(PRT_GENERAL_NAME):
        return "printer"
    if looks_like_printer(str(values.get(SYS_DESCR) or "")):
        return "printer"
    return "other"


def _tcp(ip: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _inventory_type(host: str) -> str:
    """Asset type for this address or hostname, '' when unknown or the database is not reachable."""
    try:
        from app.db import SessionLocal
        from app.models import Asset

        db = SessionLocal()
        try:
            row = db.query(Asset.type).filter((Asset.ip == host) | (Asset.hostname == host)).first()
            return str(row[0] or "") if row else ""
        finally:
            db.close()
    except Exception:
        return ""


def decide(host: str) -> str:
    """'' when port 9100 may be touched on ``host``, else the reason it may not. Uncached."""
    kind = _inventory_type(host)
    if kind:
        if "printer" in kind.lower():
            return f"{host} is a printer in the inventory; its TCP 9100 is the raw print port"
        if kind == "Linux Server":
            return ""
    answer = snmp_kind(host)
    if answer == "printer":
        return f"{host} answered SNMP as a printer; its TCP 9100 is the raw print port"
    if answer == "other":
        return ""
    for port in PRINT_PORTS:
        if _tcp(host, port):
            return f"{host} has the print port {port} open, so TCP 9100 is probably its raw print port"
    if _tcp(host, 22):
        return ""
    return (f"{host} did not answer SNMP and has no SSH, so TCP 9100 could be a printer's raw print port. "
            "Set the type to Linux Server to scrape node_exporter there.")


def blocked(target: str, port: int | str | None) -> str:
    """'' when ForgeSRE may connect to ``target``:``port``; else why not. Only port 9100 is ever blocked."""
    try:
        if int(port or 0) != RAW_PRINT_PORT:
            return ""
    except (TypeError, ValueError):
        return ""
    host = _host(target)
    if _loopback(host):
        return ""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(host)
        if hit and now - hit[0] < TTL_SECONDS:
            return hit[1]
    reason = decide(host)
    with _lock:
        _cache[host] = (now, reason)
    return reason


def blocked_url(url: str) -> str:
    """``blocked`` for an http(s) URL; '' when the URL has no port 9100."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        return blocked(parts.hostname or "", parts.port or 0)
    except ValueError:
        return ""


def note_type(host: str, asset_type: str) -> None:
    """Record what the caller already knows (an inventory row it holds), so no extra probe is needed.

    Only a printer type (block) or Linux Server (allow) is recorded; anything else is left to ``decide``.
    """
    kind = (asset_type or "").strip()
    name = _host(host)
    if not name or not kind:
        return
    if "printer" in kind.lower():
        reason = f"{name} is a printer in the inventory; its TCP 9100 is the raw print port"
    elif kind == "Linux Server":
        reason = ""
    else:
        return
    with _lock:
        _cache[name] = (time.monotonic(), reason)


def forget(host: str = "") -> None:
    """Drop cached verdicts (one host or all), e.g. after an asset's type changed."""
    with _lock:
        if host:
            _cache.pop(_host(host), None)
        else:
            _cache.clear()


def status() -> dict[str, Any]:
    """Cached verdicts in this process, for debugging which hosts are kept off TCP 9100."""
    with _lock:
        return {"cached": len(_cache), "blocked": sorted(h for h, (_t, r) in _cache.items() if r)}
