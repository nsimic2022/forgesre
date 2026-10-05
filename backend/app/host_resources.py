"""This appliance's CPU / RAM / HDD glance — not an inventory asset."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from typing import Any

NODE_EXPORTER_URL = os.environ.get("FORGESRE_NODE_EXPORTER_URL", "http://127.0.0.1:9100/metrics")
_SKIP_FS = frozenset({"tmpfs", "overlay", "squashfs", "ramfs", "devtmpfs", "proc", "sysfs", "aufs"})
_DISK_PATHS = ("/logs", "/host-generated", "/host-models", "/backups", "/")
_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')

RESOURCE_WARN_PERCENT = 80
RESOURCE_CRIT_PERCENT = 95

_cpu_sample: tuple[float, float] | None = None  # idle, total


def resource_level(percent: float | None) -> str:
    """ok / warn / crit for one appliance gauge. No reading counts as crit (blocked)."""
    if percent is None:
        return "crit"
    if percent >= RESOURCE_CRIT_PERCENT:
        return "crit"
    if percent >= RESOURCE_WARN_PERCENT:
        return "warn"
    return "ok"


def _percent(used: int | None, total: int | None) -> float | None:
    if not total or used is None:
        return None
    return round(max(0.0, min(100.0, 100.0 * used / total)), 1)


def reset_cpu_sample() -> None:
    global _cpu_sample
    _cpu_sample = None


def appliance_resources(*, node_text: str | None = None, probe_node: bool = True) -> dict[str, Any]:
    """CPU from /proc/stat (host kernel). RAM/HDD from node_exporter on this VM if present, else /proc + statvfs."""
    text = node_text
    if text is None and probe_node:
        text = fetch_node_exporter()
    parsed = parse_node_exporter(text) if text else {}

    cpu = cpu_percent()
    ram_used, ram_total, ram_source = _ram(parsed)
    hdd_used, hdd_total, hdd_mount, hdd_source = _hdd(parsed)
    sources = {ram_source, hdd_source}
    source = "node_exporter" if sources == {"node_exporter"} else (
        "mixed" if "node_exporter" in sources else "proc"
    )
    ok = ram_total is not None or hdd_total is not None or cpu is not None
    ram_percent = _percent(ram_used, ram_total)
    hdd_percent = _percent(hdd_used, hdd_total)
    net = network_status()
    return {
        "ok": bool(ok),
        "source": source,
        "cpu_percent": cpu,
        "ram_used_bytes": ram_used,
        "ram_total_bytes": ram_total,
        "hdd_used_bytes": hdd_used,
        "hdd_total_bytes": hdd_total,
        "hdd_mount": hdd_mount,
        "ram_percent": ram_percent,
        "hdd_percent": hdd_percent,
        "net": net,
        "levels": {
            "cpu": resource_level(cpu),
            "ram": resource_level(ram_percent),
            "hdd": resource_level(hdd_percent),
            "net": net["level"],
        },
        "thresholds": {"warn": RESOURCE_WARN_PERCENT, "crit": RESOURCE_CRIT_PERCENT},
    }


_VIRTUAL_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "cali", "vxlan", "tunl", "kube")
_RTF_UP = 0x0001
_RTF_REJECT = 0x0200
_SYS_NET = "/sys/class/net"


def _is_virtual_iface(name: str) -> bool:
    return not name or name.startswith(_VIRTUAL_IFACE_PREFIXES)


def _hex_ipv4(raw: str) -> str:
    try:
        n = int(raw, 16)
    except ValueError:
        return ""
    if not n:
        return ""
    return ".".join(str((n >> shift) & 0xFF) for shift in (0, 8, 16, 24))


def _default_routes_v4(text: str) -> list[tuple[int, str, str]]:
    """(metric, iface, gateway) for every UP 0.0.0.0/0 route in /proc/net/route on a non-docker iface."""
    out: list[tuple[int, str, str]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        iface, dest, gateway, flags, metric, mask = parts[0], parts[1], parts[2], parts[3], parts[6], parts[7]
        try:
            flag_bits = int(flags, 16)
            metric_n = int(metric)
        except ValueError:
            continue
        if dest != "00000000" or mask != "00000000" or not flag_bits & _RTF_UP or flag_bits & _RTF_REJECT:
            continue
        if _is_virtual_iface(iface):
            continue
        out.append((metric_n, iface, _hex_ipv4(gateway)))
    return sorted(out)


def _default_routes_v6(text: str) -> list[tuple[int, str, str]]:
    """(metric, iface, '') for every UP ::/0 route in /proc/net/ipv6_route on a non-docker iface."""
    out: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10:
            continue
        dest, plen, metric, flags, iface = parts[0], parts[1], parts[5], parts[8], parts[9]
        try:
            flag_bits = int(flags, 16)
            metric_n = int(metric, 16)
        except ValueError:
            continue
        if dest != "0" * 32 or plen != "00" or not flag_bits & _RTF_UP or flag_bits & _RTF_REJECT:
            continue
        if _is_virtual_iface(iface):
            continue
        out.append((metric_n, iface, ""))
    return sorted(out)


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _iface_link(iface: str, sys_net: str = _SYS_NET) -> str:
    """'up' / 'down' / 'unknown' for one interface from sysfs operstate (carrier when operstate is unknown)."""
    state = (_read_text(os.path.join(sys_net, iface, "operstate")) or "").strip().lower()
    if state == "up":
        return "up"
    if state in {"down", "lowerlayerdown", "notpresent", "dormant"}:
        return "down"
    carrier = (_read_text(os.path.join(sys_net, iface, "carrier")) or "").strip()
    if carrier == "1":
        return "up"
    if carrier == "0":
        return "down"
    return "unknown"


def network_status(
    *,
    route_text: str | None = None,
    ipv6_route_text: str | None = None,
    sys_net: str = _SYS_NET,
) -> dict[str, Any]:
    """This VM has net = a default route on a non-docker interface whose link is up. Green ok, red crit.

    No ping and no internet probe: an air-gapped lab with a LAN gateway is green. Red means no usable
    default route, or the interface carrying it is down. Core runs with network_mode: host, so /proc/net
    and /sys/class/net are the VM's own.
    """
    v4 = route_text if route_text is not None else _read_text("/proc/net/route")
    v6 = ipv6_route_text if ipv6_route_text is not None else _read_text("/proc/net/ipv6_route")
    routes = _default_routes_v4(v4 or "")
    family = "ipv4"
    if not routes:
        routes = _default_routes_v6(v6 or "")
        family = "ipv6"
    if v4 is None and v6 is None:
        return {"level": "crit", "iface": "", "gateway": "", "family": "", "link": "unknown",
                "reading": "no reading", "detail": "/proc/net/route not readable"}
    if not routes:
        return {"level": "crit", "iface": "", "gateway": "", "family": "", "link": "unknown",
                "reading": "no default route", "detail": "no default route on a non-docker interface"}
    for _metric, iface, gateway in routes:
        link = _iface_link(iface, sys_net)
        if link != "down":
            reading = f"{iface} up" + (f" · gw {gateway}" if gateway else "")
            detail = "default route present" + ("; link state unreadable" if link == "unknown" else "")
            return {"level": "ok", "iface": iface, "gateway": gateway, "family": family, "link": link,
                    "reading": reading, "detail": detail}
    iface = routes[0][1]
    return {"level": "crit", "iface": iface, "gateway": routes[0][2], "family": family, "link": "down",
            "reading": f"{iface} down", "detail": "default route interface link is down"}


def fetch_node_exporter(url: str = NODE_EXPORTER_URL, timeout: float = 0.25) -> str | None:
    """Local node_exporter on the appliance VM (host network). Not an inventory scrape."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read(2_000_000)
        return raw.decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def parse_node_exporter(text: str) -> dict[str, Any]:
    ram_avail = ram_total = None
    filesystems: list[tuple[str, str, float, float]] = []
    size_by: dict[tuple[str, str], float] = {}
    avail_by: dict[tuple[str, str], float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        name = match.group("name")
        value = float(match.group("value"))
        labels = _parse_labels(match.group("labels") or "")
        if name == "node_memory_MemAvailable_bytes":
            ram_avail = value
        elif name == "node_memory_MemTotal_bytes":
            ram_total = value
        elif name in {"node_filesystem_size_bytes", "node_filesystem_avail_bytes"}:
            fstype = labels.get("fstype") or ""
            mount = labels.get("mountpoint") or ""
            if fstype in _SKIP_FS or not mount:
                continue
            key = (mount, fstype)
            if name.endswith("size_bytes"):
                size_by[key] = value
            else:
                avail_by[key] = value
    for key, size in size_by.items():
        avail = avail_by.get(key)
        if avail is None or size <= 0:
            continue
        filesystems.append((key[0], key[1], size, avail))
    hdd = _pick_filesystem(filesystems)
    out: dict[str, Any] = {}
    if ram_total and ram_total > 0 and ram_avail is not None:
        out["ram_used_bytes"] = max(0, int(ram_total - ram_avail))
        out["ram_total_bytes"] = int(ram_total)
    if hdd is not None:
        mount, _fstype, size, avail = hdd
        out["hdd_used_bytes"] = max(0, int(size - avail))
        out["hdd_total_bytes"] = int(size)
        out["hdd_mount"] = mount
    return out


def cpu_percent() -> float | None:
    """Host CPU busy percent from /proc/stat deltas; loadavg on the first sample."""
    global _cpu_sample
    current = _read_proc_stat()
    if current is None:
        return _cpu_from_load()
    idle, total = current
    prev = _cpu_sample
    _cpu_sample = current
    if prev is None:
        return _cpu_from_load()
    prev_idle, prev_total = prev
    d_total = total - prev_total
    d_idle = idle - prev_idle
    if d_total <= 0:
        return _cpu_from_load()
    busy = 100.0 * (1.0 - (d_idle / d_total))
    return round(max(0.0, min(100.0, busy)), 1)


def _ram(parsed: dict[str, Any]) -> tuple[int | None, int | None, str]:
    if parsed.get("ram_total_bytes"):
        return int(parsed["ram_used_bytes"]), int(parsed["ram_total_bytes"]), "node_exporter"
    info = _read_meminfo()
    if info is None:
        return None, None, "proc"
    return info[0], info[1], "proc"


def _hdd(parsed: dict[str, Any]) -> tuple[int | None, int | None, str | None, str]:
    if parsed.get("hdd_total_bytes"):
        return (
            int(parsed["hdd_used_bytes"]),
            int(parsed["hdd_total_bytes"]),
            str(parsed.get("hdd_mount") or "/"),
            "node_exporter",
        )
    best: tuple[int, int, str] | None = None
    extra = []
    logs = (os.environ.get("FORGESRE_LOGS_DIR") or "").strip()
    if logs:
        extra.append(logs)
    for path in (*extra, *_DISK_PATHS):
        row = _statvfs_disk(path)
        if row is None:
            continue
        used, total, mount = row
        if best is None or total > best[1]:
            best = (used, total, mount)
    if best is None:
        return None, None, None, "proc"
    return best[0], best[1], best[2], "proc"


def _pick_filesystem(rows: list[tuple[str, str, float, float]]) -> tuple[str, str, float, float] | None:
    if not rows:
        return None
    rooted = [row for row in rows if row[0] == "/"]
    pool = rooted or rows
    return max(pool, key=lambda row: row[2])


def _parse_labels(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _LABEL_RE.finditer(blob):
        out[match.group(1)] = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
    return out


def _read_proc_stat() -> tuple[float, float] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            line = fh.readline()
    except OSError:
        return None
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    nums = [float(x) for x in parts[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0.0)
    total = sum(nums[:8])
    return idle, total


def _cpu_from_load() -> float | None:
    try:
        load1, _, _ = os.getloadavg()
        n = os.cpu_count() or 1
        return round(max(0.0, min(100.0, 100.0 * load1 / n)), 1)
    except OSError:
        return None


def _read_meminfo() -> tuple[int, int] | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    total_kb = avail_kb = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])
        if total_kb is not None and avail_kb is not None:
            break
    if not total_kb:
        return None
    total = total_kb * 1024
    avail = (avail_kb or 0) * 1024
    return max(0, total - avail), total


def _statvfs_disk(path: str) -> tuple[int, int, str] | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    fr = st.f_frsize or st.f_bsize
    total = int(fr * st.f_blocks)
    if total <= 0:
        return None
    avail = int(fr * st.f_bavail)
    return max(0, total - avail), total, path
