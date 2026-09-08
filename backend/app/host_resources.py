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

_cpu_sample: tuple[float, float] | None = None  # idle, total


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
    return {
        "ok": bool(ok),
        "source": source,
        "cpu_percent": cpu,
        "ram_used_bytes": ram_used,
        "ram_total_bytes": ram_total,
        "hdd_used_bytes": hdd_used,
        "hdd_total_bytes": hdd_total,
        "hdd_mount": hdd_mount,
    }


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
