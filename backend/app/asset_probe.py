"""Host reachability probe for the operator CLI.

ICMP ping only proves L3. ForgeSRE "sees" a host when Prometheus can scrape
exporter /metrics (Linux node_exporter :9100, Windows windows_exporter :9182).
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

LINUX_EXPORTER_PORT = 9100
WINDOWS_EXPORTER_PORT = 9182
DEFAULT_TIMEOUT = 2.0
METRICS_PATH = "/metrics"

PingRunner = Callable[[str, float], tuple[int, str, str]]
MetricsFetcher = Callable[[str, float], tuple[int | None, str, str]]


def asset_kind(type: str = "", profile: str = "") -> str:
    """linux | windows | network | web | other. Windows is checked before 'server'."""
    blob = f"{type} {profile}".lower()
    if "windows" in blob or "win32" in blob:
        return "windows"
    if "linux" in blob:
        return "linux"
    if "network" in blob or "switch" in blob or "router" in blob or "firewall" in blob:
        return "network"
    if "web" in blob or "appliance" in blob:
        return "web"
    if "server" in blob:
        return "linux"
    return "other"


def default_monitoring_profile(type: str, profile: str = "") -> str:
    if (profile or "").strip():
        return profile.strip()
    kind = asset_kind(type)
    if kind == "windows":
        return "windows-standard"
    if kind == "linux":
        return "linux-standard"
    if kind == "web":
        return "web-standard"
    return "network-switch"


def default_exporter_port(type: str = "", profile: str = "") -> int | None:
    kind = asset_kind(type, profile)
    if kind == "windows":
        return WINDOWS_EXPORTER_PORT
    if kind == "linux":
        return LINUX_EXPORTER_PORT
    return None


def default_scrape_address(type: str, ip: str, profile: str = "") -> str:
    ip = (ip or "").strip()
    port = default_exporter_port(type, profile)
    if not ip or port is None:
        return ""
    return f"{ip}:{port}"


def parse_host_port(address: str) -> tuple[str, int | None]:
    text = (address or "").strip()
    if not text:
        return "", None
    if text.startswith("[") and "]" in text:
        host, _, rest = text[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if text.count(":") == 1:
        host, _, port = text.rpartition(":")
        if host and port.isdigit():
            return host, int(port)
    return text, None


def looks_like_host(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    host, _port = parse_host_port(text)
    candidate = host or text
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return "." in candidate or candidate.replace("-", "").isalnum()


def exporter_label(port: int | None) -> str:
    if port == WINDOWS_EXPORTER_PORT:
        return "windows_exporter"
    if port == LINUX_EXPORTER_PORT:
        return "node_exporter"
    return "exporter"


@dataclass
class CheckResult:
    name: str
    ok: bool | None
    detail: str
    elapsed_ms: int = 0

    @property
    def mark(self) -> str:
        if self.ok is True:
            return "PASS"
        if self.ok is False:
            return "FAIL"
        return "SKIP"


@dataclass
class AssetProbe:
    asset_id: str
    hostname: str
    ip: str
    kind: str
    type: str
    scrape: str
    port: int | None
    icmp: CheckResult
    metrics: CheckResult
    extra: list[CheckResult] = field(default_factory=list)
    hint: str = ""

    @property
    def overall(self) -> str:
        if self.metrics.ok is True:
            return "PASS"
        if self.metrics.ok is False:
            return "FAIL"
        if self.icmp.ok is True:
            return "WARN"
        if self.icmp.ok is False:
            return "FAIL"
        return "SKIP"


def _run_ping(host: str, timeout: float) -> tuple[int, str, str]:
    wait = max(1, int(round(timeout)))
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", str(wait), host],
            capture_output=True,
            text=True,
            timeout=timeout + 1.5,
        )
    except FileNotFoundError:
        return 127, "", "ping not installed (iputils-ping)"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout {timeout}s"
    return result.returncode, result.stdout or "", result.stderr or ""


def _fetch_metrics(url: str, timeout: float) -> tuple[int | None, str, str]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "forgesre-ping/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2048)
            preview = body.decode("utf-8", errors="replace")
            return int(getattr(response, "status", 200) or 200), preview, ""
    except socket.timeout:
        return None, "", f"timeout {timeout}s"
    except TimeoutError:
        return None, "", f"timeout {timeout}s"
    except urllib.error.HTTPError as exc:
        return int(exc.code), "", f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc) or exc)
        lowered = reason.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return None, "", f"timeout {timeout}s"
        return None, "", reason
    except OSError as exc:
        return None, "", str(exc)


def probe_icmp(host: str, timeout: float = DEFAULT_TIMEOUT, runner: PingRunner | None = None) -> CheckResult:
    host = (host or "").strip()
    if not host:
        return CheckResult("icmp", None, "no IP", 0)
    started = time.monotonic()
    code, stdout, stderr = (runner or _run_ping)(host, timeout)
    elapsed = int((time.monotonic() - started) * 1000)
    if code == 0:
        rtt = ""
        for token in (stdout or "").replace("=", " ").split():
            if token.endswith("ms") and token[:-2].replace(".", "").isdigit():
                rtt = token
                break
        detail = f"reachable{f' {rtt}' if rtt else ''} ({elapsed}ms)"
        return CheckResult("icmp", True, detail, elapsed)
    err = (stderr or stdout or "").strip().splitlines()
    message = err[-1] if err else f"exit {code}"
    if code in {124, 1, 2} and "timeout" not in message.lower():
        message = f"no reply ({elapsed}ms)"
    if "timeout" in message.lower() or code == 124:
        message = f"timeout {timeout}s"
    return CheckResult("icmp", False, message, elapsed)


def looks_like_prometheus(preview: str) -> bool:
    text = preview or ""
    if "# HELP" in text or "# TYPE" in text:
        return True
    return any(token in text for token in ("node_", "windows_", "go_", "process_"))


def probe_metrics(
    host: str,
    port: int,
    timeout: float = DEFAULT_TIMEOUT,
    fetcher: MetricsFetcher | None = None,
) -> CheckResult:
    url = f"http://{host}:{port}{METRICS_PATH}"
    started = time.monotonic()
    status, preview, error = (fetcher or _fetch_metrics)(url, timeout)
    elapsed = int((time.monotonic() - started) * 1000)
    label = exporter_label(port)
    if status == 200:
        kind = "prometheus text" if looks_like_prometheus(preview) else "HTTP 200"
        return CheckResult("metrics", True, f"{label} :{port}/metrics {kind} ({elapsed}ms)", elapsed)
    if status:
        return CheckResult("metrics", False, f"{label} :{port}/metrics HTTP {status} ({elapsed}ms)", elapsed)
    err = error or "unreachable"
    return CheckResult("metrics", False, f"{label} :{port}/metrics {err}", elapsed)


def resolve_probe(item: dict[str, Any]) -> tuple[str, int | None, str]:
    """Return (host, port, scrape_display) for an inventory row or ad-hoc target."""
    scrape = str(item.get("scrape_address") or "").strip()
    ip = str(item.get("ip") or "").strip()
    hostname = str(item.get("hostname") or item.get("asset_id") or "").strip()
    kind_type = str(item.get("type") or "")
    profile = str(item.get("monitoring_profile") or "")
    host = ip or hostname
    port: int | None = None
    if scrape:
        scrape_host, scrape_port = parse_host_port(scrape)
        host = scrape_host or host
        port = scrape_port
    if port is None:
        port = default_exporter_port(kind_type, profile)
        if port is not None and host:
            scrape = scrape or f"{host}:{port}"
    return host, port, scrape


def hint_for(result: AssetProbe) -> str:
    if result.metrics.ok is True:
        if result.icmp.ok is False:
            return (
                f"{result.asset_id}: /metrics works but ICMP failed. ForgeSRE can scrape this host; "
                "ping is blocked or unused."
            )
        return ""
    if result.metrics.ok is None:
        if result.icmp.ok is True:
            return (
                f"{result.asset_id}: ICMP reachable, no HTTP exporter. Network gear is scraped with "
                "SNMP UDP/161 (`./forgesre snmp`), not :9100/:9182."
            )
        if result.icmp.ok is False:
            return f"{result.asset_id}: ICMP failed. Host down, wrong IP, or ICMP blocked from this VM."
        return ""
    kind = result.kind
    port = result.port
    if kind == "windows" or port == WINDOWS_EXPORTER_PORT:
        return (
            f"{result.asset_id}: ICMP {'ok' if result.icmp.ok else 'failed'}, "
            "windows_exporter :9182/metrics failed. ForgeSRE will not show this host until Prometheus "
            "scrapes /metrics. Typical: windows_exporter not running, Windows firewall TCP 9182, or "
            "scrape_address still :9100 (Linux node_exporter)."
        )
    if kind == "linux" or port == LINUX_EXPORTER_PORT:
        return (
            f"{result.asset_id}: ICMP {'ok' if result.icmp.ok else 'failed'}, "
            "node_exporter :9100/metrics failed. Ping is not a Prometheus scrape. Typical: "
            "node_exporter down, firewall TCP 9100, or this is Windows (use type Windows Server / :9182)."
        )
    return (
        f"{result.asset_id}: ICMP {'ok' if result.icmp.ok else 'failed'}, exporter /metrics failed "
        f"on :{port}."
    )


def probe_target(
    item: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    ping_runner: PingRunner | None = None,
    metrics_fetcher: MetricsFetcher | None = None,
) -> AssetProbe:
    kind = asset_kind(str(item.get("type") or ""), str(item.get("monitoring_profile") or ""))
    host, port, scrape = resolve_probe(item)
    icmp = probe_icmp(host, timeout, runner=ping_runner)
    extra: list[CheckResult] = []
    probe_both = str(item.get("_probe_both") or "") == "1"
    if kind == "network" and not scrape and not probe_both:
        metrics = CheckResult("metrics", None, "SNMP UDP/161 — ./forgesre snmp", 0)
    elif probe_both and host:
        linux_m = probe_metrics(host, LINUX_EXPORTER_PORT, timeout, fetcher=metrics_fetcher)
        win_m = probe_metrics(host, WINDOWS_EXPORTER_PORT, timeout, fetcher=metrics_fetcher)
        if linux_m.ok:
            metrics, extra = linux_m, [win_m]
        elif win_m.ok:
            metrics, extra = win_m, [linux_m]
            kind = "windows"
            port = WINDOWS_EXPORTER_PORT
            scrape = f"{host}:{WINDOWS_EXPORTER_PORT}"
        else:
            metrics, extra = linux_m, [win_m]
            port = port or LINUX_EXPORTER_PORT
    elif not host or port is None:
        metrics = CheckResult("metrics", None, "no scrape port (set scrape_address or type)", 0)
    else:
        metrics = probe_metrics(host, port, timeout, fetcher=metrics_fetcher)
        if metrics.ok is False and kind == "windows" and port != WINDOWS_EXPORTER_PORT and host:
            extra.append(probe_metrics(host, WINDOWS_EXPORTER_PORT, timeout, fetcher=metrics_fetcher))
        elif metrics.ok is False and str(item.get("_also_try_windows") or "") == "1" and host:
            if port != WINDOWS_EXPORTER_PORT:
                extra.append(probe_metrics(host, WINDOWS_EXPORTER_PORT, timeout, fetcher=metrics_fetcher))
    result = AssetProbe(
        asset_id=str(item.get("asset_id") or item.get("hostname") or host or "target"),
        hostname=str(item.get("hostname") or ""),
        ip=str(item.get("ip") or host or ""),
        kind=kind,
        type=str(item.get("type") or kind or ""),
        scrape=scrape,
        port=port,
        icmp=icmp,
        metrics=metrics,
        extra=extra,
    )
    result.hint = hint_for(result)
    if extra and extra[0].ok is True and result.metrics.ok is False:
        result.hint = (
            f"{result.asset_id}: scrape_address {scrape or port} failed but windows_exporter "
            f":{WINDOWS_EXPORTER_PORT}/metrics worked. Set type to Windows Server and "
            f"scrape_address={host}:{WINDOWS_EXPORTER_PORT}."
        )
    return result


def ad_hoc_item(selector: str) -> dict[str, Any]:
    selector = selector.strip()
    host, port = parse_host_port(selector)
    host = host or selector
    item: dict[str, Any] = {
        "asset_id": selector,
        "hostname": host,
        "ip": host,
        "type": "",
        "scrape_address": "",
        "_also_try_windows": "1",
    }
    if port is not None:
        item["scrape_address"] = f"{host}:{port}"
        if port == WINDOWS_EXPORTER_PORT:
            item["type"] = "Windows Server"
        elif port == LINUX_EXPORTER_PORT:
            item["type"] = "Linux Server"
    else:
        item["_probe_both"] = "1"
    return item


def select_assets(
    rows: list[dict[str, Any]],
    selector: str = "",
    *,
    include_demo: bool = False,
    is_demo: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    demo_fn = is_demo or (lambda _row: False)
    skipped_demo = 0
    chosen: list[dict[str, Any]] = []
    selector = (selector or "").strip()
    for row in rows:
        if not isinstance(row, dict):
            continue
        demo = demo_fn(row)
        if selector:
            identity = {
                str(row.get("asset_id") or "").lower(),
                str(row.get("hostname") or "").lower(),
                str(row.get("ip") or "").lower(),
            }
            scrape = str(row.get("scrape_address") or "").lower()
            hit = selector.lower() in identity or scrape == selector.lower() or scrape.startswith(
                selector.lower() + ":"
            )
            if not hit:
                continue
            chosen.append(row)
            continue
        if demo and not include_demo:
            skipped_demo += 1
            continue
        if not (row.get("ip") or row.get("scrape_address") or row.get("hostname")):
            continue
        chosen.append(row)
    return chosen, skipped_demo


def format_report(
    results: list[AssetProbe],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    skipped_demo: int = 0,
    color: bool = False,
) -> str:
    from app.cli_view import BOLD, DIM, GREEN, RED, YELLOW, paint

    lines = [
        paint("ForgeSRE asset probe", BOLD, color),
        f"timeout {timeout}s per check. ICMP ping is L3 only — ForgeSRE sees a host when /metrics scrapes.",
        "Linux node_exporter :9100/metrics. Windows windows_exporter :9182/metrics.",
        "",
        f"{'ASSET':<22} {'IP':<16} {'ICMP':<6} {'METRICS'}",
        "-" * 78,
    ]
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    hints: list[str] = []
    for row in results:
        counts[row.overall] = counts.get(row.overall, 0) + 1
        mark_color = {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW, "SKIP": DIM}.get(row.overall, "")
        icmp = paint(f"{row.icmp.mark:<6}", GREEN if row.icmp.ok else (RED if row.icmp.ok is False else DIM), color)
        metrics_color = GREEN if row.metrics.ok else (RED if row.metrics.ok is False else DIM)
        metrics = paint(row.metrics.mark, metrics_color, color)
        lines.append(
            f"{row.asset_id[:22]:<22} {row.ip[:16]:<16} {icmp} {metrics}  {row.metrics.detail}"
        )
        for extra in row.extra:
            extra_c = GREEN if extra.ok else (RED if extra.ok is False else DIM)
            lines.append(f"{'':<22} {'':<16} {'':<6} {paint(extra.mark, extra_c, color)}  also {extra.detail}")
        if row.hint:
            hints.append(row.hint)
    lines.append("")
    summary = (
        f"{counts['PASS']} PASS  {counts['FAIL']} FAIL  {counts['WARN']} WARN  {counts['SKIP']} SKIP"
    )
    lines.append(paint(summary, RED if counts["FAIL"] else GREEN, color))
    if skipped_demo:
        lines.append(
            paint(
                f"skipped {skipped_demo} demo lab asset(s). Include with: ./forgesre ping --demo",
                DIM,
                color,
            )
        )
    if hints:
        lines.append("")
        lines.append("How to read ICMP ok / metrics fail:")
        for hint in hints:
            lines.append(f"  {hint}")
    return "\n".join(lines) + "\n"


def overall_exit(results: list[AssetProbe]) -> int:
    if any(row.overall == "FAIL" for row in results):
        return 1
    return 0
