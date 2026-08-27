"""Host reachability probe for the operator CLI and Assets list badges.

ICMP ping only proves L3. ForgeSRE "sees" a host when Prometheus can scrape
exporter /metrics (Linux node_exporter :9100, Windows windows_exporter :9182)
or snmp_exporter walks UDP/161 for network devices.
"""

from __future__ import annotations

import ipaddress
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.exporter_detect import detect_exporter, fetch_metrics, is_auto_asset_type

LINUX_EXPORTER_PORT = 9100
WINDOWS_EXPORTER_PORT = 9182
DEFAULT_TIMEOUT = 2.0
LIST_PROBE_TIMEOUT = 0.8
LIST_PROBE_FRESH_SECONDS = 20
METRICS_PATH = "/metrics"

PingRunner = Callable[[str, float], tuple[int, str, str]]
MetricsFetcher = Callable[[str, float], tuple[int | None, str, str]]
SnmpProber = Callable[..., bool]


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
    preview: str = ""

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

    @property
    def ping_color(self) -> str:
        return ping_badge_color(self.icmp.ok, scrape_answers(self.metrics.ok, self.extra))


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
    return fetch_metrics(url, timeout, user_agent="forgesre-ping/1")


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
        return CheckResult(
            "metrics",
            True,
            f"{label} :{port}/metrics {kind} ({elapsed}ms)",
            elapsed,
            preview=preview or "",
        )
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


def probe_snmp(
    host: str,
    timeout: float = DEFAULT_TIMEOUT,
    prober: SnmpProber | None = None,
) -> CheckResult:
    host = (host or "").strip()
    if not host:
        return CheckResult("metrics", None, "no IP", 0)
    started = time.monotonic()
    if prober is not None:
        ok = bool(prober(host, timeout))
    else:
        from discovery import probe_snmp_udp

        ok = bool(probe_snmp_udp(host, timeout=timeout))
    elapsed = int((time.monotonic() - started) * 1000)
    if ok:
        return CheckResult("metrics", True, f"SNMP UDP/161 sysDescr ({elapsed}ms)", elapsed)
    return CheckResult("metrics", False, f"SNMP UDP/161 no reply ({elapsed}ms)", elapsed)


def check_color(ok: bool | None) -> str:
    if ok is True:
        return "green"
    if ok is False:
        return "red"
    return "yellow"


def scrape_answers(metrics_ok: bool | None, extra: list[CheckResult] | None = None) -> bool:
    """True when /metrics or SNMP (or a fallback exporter port) answered."""
    if metrics_ok is True:
        return True
    return any(item.ok is True for item in (extra or []))


def ping_badge_color(icmp_ok: bool | None, scrape_ok: bool) -> str:
    """Ping pill: green ICMP; yellow ICMP-fail but scrape works; red both down.

    Typical Windows: ICMP unused/blocked, windows_exporter :9182 still scrapes.
    Do not require opening ICMP or SSH/22.
    """
    if icmp_ok is True:
        return "green"
    if icmp_ok is False:
        return "yellow" if scrape_ok else "red"
    return "yellow"


def ping_badge_from_stored(ping: str | None, exporter: str | None) -> str:
    """Last-known ping, remapping old ICMP-only red when exporter is already green."""
    ping = (ping or "yellow").strip() or "yellow"
    exporter = (exporter or "yellow").strip() or "yellow"
    if ping == "green":
        return "green"
    if ping == "red" and exporter == "green":
        return "yellow"
    return ping


def ping_detail_from_probe(probe: AssetProbe) -> str:
    detail = (probe.icmp.detail or "").strip()
    if probe.icmp.ok is False and scrape_answers(probe.metrics.ok, probe.extra):
        note = "ICMP unused/blocked, exporter reachable"
        if note.lower() not in detail.lower():
            detail = f"{detail}; {note}" if detail else note
    return detail[:255]


def exporter_badge_label(item: dict[str, Any] | Any, port: int | None = None) -> str:
    if not isinstance(item, dict):
        kind = asset_kind(getattr(item, "type", "") or "", getattr(item, "monitoring_profile", "") or "")
        scrape = str(getattr(item, "scrape_address", "") or "")
        if port is None:
            _host, port = parse_host_port(scrape)
            if port is None:
                port = default_exporter_port(
                    str(getattr(item, "type", "") or ""),
                    str(getattr(item, "monitoring_profile", "") or ""),
                )
    else:
        kind = asset_kind(str(item.get("type") or ""), str(item.get("monitoring_profile") or ""))
        scrape = str(item.get("scrape_address") or "")
        if port is None:
            _host, port = parse_host_port(scrape)
            if port is None:
                port = default_exporter_port(str(item.get("type") or ""), str(item.get("monitoring_profile") or ""))
    if kind == "network":
        return "SNMP"
    if port == WINDOWS_EXPORTER_PORT:
        return ":9182"
    if port == LINUX_EXPORTER_PORT:
        return ":9100"
    if port:
        return f":{port}"
    return "exp."


def _checked_age_seconds(checked: datetime | None, now: datetime) -> float:
    if checked is None:
        return 10**9
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - checked).total_seconds()


def apply_probe_to_asset(asset: Any, probe: AssetProbe) -> None:
    from app.models import utcnow

    asset.ping_status = probe.ping_color
    asset.ping_detail = ping_detail_from_probe(probe)
    asset.exporter_status = check_color(probe.metrics.ok)
    asset.exporter_detail = (probe.metrics.detail or "")[:255]
    asset.probe_checked_at = utcnow()


def reachability_snapshot(asset: Any, probe: AssetProbe | None = None) -> dict[str, Any]:
    if probe is not None:
        ping = probe.ping_color
        exporter = check_color(probe.metrics.ok)
        ping_detail = ping_detail_from_probe(probe)
        exporter_detail = probe.metrics.detail or ""
        label = exporter_badge_label(asset, probe.port)
        checked = getattr(asset, "probe_checked_at", None)
    else:
        exporter = getattr(asset, "exporter_status", None) or "yellow"
        ping = ping_badge_from_stored(getattr(asset, "ping_status", None), exporter)
        ping_detail = getattr(asset, "ping_detail", None) or "not probed yet"
        exporter_detail = getattr(asset, "exporter_detail", None) or "not probed yet"
        label = exporter_badge_label(asset)
        checked = getattr(asset, "probe_checked_at", None)
    return {
        "asset_id": getattr(asset, "asset_id", ""),
        "ip": getattr(asset, "ip", "") or "",
        "ping": ping,
        "ping_detail": ping_detail,
        "exporter": exporter,
        "exporter_detail": exporter_detail,
        "exporter_label": label,
        "checked_at": checked.isoformat() if checked else None,
    }


def asset_as_probe_item(asset: Any) -> dict[str, Any]:
    return {
        "asset_id": getattr(asset, "asset_id", "") or "",
        "hostname": getattr(asset, "hostname", "") or "",
        "ip": getattr(asset, "ip", "") or "",
        "type": getattr(asset, "type", "") or "",
        "monitoring_profile": getattr(asset, "monitoring_profile", "") or "",
        "scrape_address": getattr(asset, "scrape_address", "") or "",
    }


def refresh_reachability(
    assets: list[Any],
    *,
    timeout: float = LIST_PROBE_TIMEOUT,
    max_workers: int = 8,
    force: bool = False,
    ping_runner: PingRunner | None = None,
    metrics_fetcher: MetricsFetcher | None = None,
    snmp_prober: SnmpProber | None = None,
    probe_fn: Callable[..., AssetProbe] | None = None,
) -> list[dict[str, Any]]:
    """Probe stale rows off the request thread pool; persist last-known colors.

    The Assets HTML page must not wait on this. Call it from the JSON endpoint
    after the table has already rendered last-known (yellow if never probed).
    """
    now = datetime.now(timezone.utc)
    stale: list[Any] = []
    for asset in assets:
        checked = getattr(asset, "probe_checked_at", None)
        if force or _checked_age_seconds(checked, now) >= LIST_PROBE_FRESH_SECONDS:
            stale.append(asset)
    run = probe_fn or probe_target
    if stale:
        jobs = [(asset, asset_as_probe_item(asset)) for asset in stale]
        workers = max(1, min(max_workers, len(jobs)))

        def _one(job: tuple[Any, dict[str, Any]]) -> tuple[Any, AssetProbe]:
            asset, item = job
            return asset, run(
                item,
                timeout=timeout,
                ping_runner=ping_runner,
                metrics_fetcher=metrics_fetcher,
                snmp_prober=snmp_prober,
            )

        if workers == 1:
            pairs = [_one(job) for job in jobs]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pairs = list(pool.map(_one, jobs))
        for asset, probe in pairs:
            apply_probe_to_asset(asset, probe)
    return [reachability_snapshot(asset) for asset in assets]


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
    if kind == "network":
        return (
            f"{result.asset_id}: ICMP {'ok' if result.icmp.ok else 'failed'}, "
            "SNMP UDP/161 failed. snmp_exporter walks this IP after the row is "
            "type Network device. Check community/ACL (`./forgesre snmp`)."
        )
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


def _metrics_from_detect(
    port: int,
    family_ok: bool,
    status: int | None,
    error: str,
    preview: str = "",
) -> CheckResult:
    label = exporter_label(port)
    if family_ok:
        return CheckResult(
            "metrics",
            True,
            f"{label} :{port}/metrics prometheus text",
            0,
            preview=preview or "",
        )
    if status == 200:
        return CheckResult(
            "metrics",
            False,
            f"{label} :{port}/metrics HTTP 200 (not node_/windows_ metrics)",
            0,
            preview=preview or "",
        )
    if status:
        return CheckResult("metrics", False, f"{label} :{port}/metrics HTTP {status}", 0)
    return CheckResult("metrics", False, f"{label} :{port}/metrics {error or 'unreachable'}", 0)


def probe_target(
    item: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    ping_runner: PingRunner | None = None,
    metrics_fetcher: MetricsFetcher | None = None,
    snmp_prober: SnmpProber | None = None,
) -> AssetProbe:
    kind = asset_kind(str(item.get("type") or ""), str(item.get("monitoring_profile") or ""))
    host, port, scrape = resolve_probe(item)
    icmp = probe_icmp(host, timeout, runner=ping_runner)
    extra: list[CheckResult] = []
    probe_both = str(item.get("_probe_both") or "") == "1"
    detect_message = ""
    if not probe_both and kind != "network" and host and port is None:
        probe_both = True
    if kind == "network" and not scrape and not probe_both:
        metrics = probe_snmp(host, timeout, prober=snmp_prober)
    elif probe_both and host:
        detected = detect_exporter(
            host,
            hint_type=str(item.get("type") or ""),
            hint_profile=str(item.get("monitoring_profile") or ""),
            timeout=timeout,
            fetcher=metrics_fetcher,
        )
        linux_m = _metrics_from_detect(
            LINUX_EXPORTER_PORT,
            detected.linux,
            detected.linux_status,
            detected.linux_error,
            preview=detected.linux_preview,
        )
        win_m = _metrics_from_detect(
            WINDOWS_EXPORTER_PORT,
            detected.windows,
            detected.windows_status,
            detected.windows_error,
            preview=detected.windows_preview,
        )
        if detected.kind == "windows":
            metrics, extra = win_m, [linux_m]
            kind = "windows"
            port = WINDOWS_EXPORTER_PORT
            scrape = detected.scrape_address or f"{host}:{WINDOWS_EXPORTER_PORT}"
        elif detected.kind == "linux":
            metrics, extra = linux_m, [win_m]
            kind = "linux"
            port = LINUX_EXPORTER_PORT
            scrape = detected.scrape_address or f"{host}:{LINUX_EXPORTER_PORT}"
        else:
            extra = [linux_m, win_m]
            metrics = CheckResult(
                "metrics",
                None,
                "no scrape port (tried :9100 and :9182; no node_/windows_)",
                0,
            )
            detect_message = detected.message
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
    if detect_message and result.metrics.ok is not True:
        result.hint = f"{result.asset_id}: {detect_message}"
    return result


LIVE_CLASS_TYPE = {
    "linux": ("Linux Server", "linux-standard"),
    "windows": ("Windows Server", "windows-standard"),
}


def classification_patch(item: dict[str, Any], probe: AssetProbe) -> dict[str, str]:
    """Fields to persist so Prometheus HTTP SD can scrape a live Linux/Windows exporter.

    Used when the operator saved IP-only / Auto / Unknown and forgot scrape_address.
    Does not rewrite Network device. Does not invent a host.
    """
    if probe.metrics.ok is not True or probe.kind not in LIVE_CLASS_TYPE:
        return {}
    saved_kind = asset_kind(str(item.get("type") or ""), str(item.get("monitoring_profile") or ""))
    if saved_kind == "network":
        return {}
    type_name, profile = LIVE_CLASS_TYPE[probe.kind]
    scrape = (probe.scrape or "").strip()
    current_scrape = str(item.get("scrape_address") or "").strip()
    current_type = str(item.get("type") or "").strip()
    auto = is_auto_asset_type(current_type)
    unknown = (not current_type) or current_type.lower() == "unknown" or saved_kind in {
        "other",
        "web",
        "unknown",
    }
    patch: dict[str, str] = {}
    if auto or unknown or saved_kind not in {"linux", "windows"}:
        if current_type != type_name:
            patch["type"] = type_name
        current_profile = str(item.get("monitoring_profile") or "").strip()
        if current_profile != profile and current_profile in {
            "",
            "linux-standard",
            "windows-standard",
            "network-switch",
        }:
            patch["monitoring_profile"] = profile
        elif not current_profile:
            patch["monitoring_profile"] = profile
    if scrape and not current_scrape:
        patch["scrape_address"] = scrape
    elif scrape and (auto or unknown) and current_scrape != scrape:
        patch["scrape_address"] = scrape
    return patch


def apply_live_class_to_item(item: dict[str, Any], probe: AssetProbe) -> dict[str, Any]:
    """Copy live linux/windows class onto a verify/ping item (no DB)."""
    out = dict(item)
    patch = classification_patch(item, probe)
    if not patch:
        return out
    out.update(patch)
    out["_classified_live"] = "1"
    return out


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


def asset_selector_hit(row: dict[str, Any], selector: str) -> bool:
    """Match inventory number, id, hostname, IP, or scrape host."""
    sel = (selector or "").strip().lower()
    if not sel:
        return False
    number = row.get("number")
    if sel.isdigit() and number is not None and str(number) == sel:
        return True
    identity = {
        str(row.get("asset_id") or "").lower(),
        str(row.get("hostname") or "").lower(),
        str(row.get("ip") or "").lower(),
    }
    scrape = str(row.get("scrape_address") or "").lower()
    return sel in identity or scrape == sel or scrape.startswith(sel + ":")


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
            if not asset_selector_hit(row, selector):
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
        "Linux node_exporter :9100/metrics. Windows windows_exporter :9182/metrics. Network: SNMP UDP/161.",
        "",
        f"{'ASSET':<22} {'IP':<16} {'ICMP':<6} {'METRICS'}",
        "-" * 78,
    ]
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    hints: list[str] = []
    for row in results:
        counts[row.overall] = counts.get(row.overall, 0) + 1
        mark_color = {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW, "SKIP": DIM}.get(row.overall, "")
        ping_paint = {"green": GREEN, "yellow": YELLOW, "red": RED}.get(row.ping_color, DIM)
        icmp = paint(f"{row.icmp.mark:<6}", ping_paint, color)
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
