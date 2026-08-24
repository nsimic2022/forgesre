"""Cheap Linux / Windows / network detect from the ForgeSRE host.

GET ``http://<ip>:9182/metrics`` and ``http://<ip>:9100/metrics`` with a short
timeout. ICMP ping is a reachability hint only — it does not pick an OS or
mark a device as Network.

Classification:

- ``:9182`` answers with windows_exporter / ``windows_`` metrics → Windows Server,
  scrape ``:9182``.
- ``:9100`` answers with node_exporter / ``node_`` metrics → Linux Server,
  scrape ``:9100``.
- Both: prefer the saved asset type when it is already Linux or Windows; else
  prefer family signals (``windows_`` vs ``node_uname`` / ``node_cpu``). If both
  families are strong and no type is saved, prefer Windows Server ``:9182``
  (mis-classifying Windows as Linux was the original scrape miss).
- Neither HTTP family: **do not guess Network**. Mark Network only when
  discovery already used SNMP (UDP/161 GET / ``snmp_ok``) or a live SNMP
  prober (the same path snmp_exporter uses after Approve) answered. Missing
  :9100/:9182 is not a fingerprint.
- Saved Linux/Windows is not rewritten to Network by SNMP.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

LINUX_EXPORTER_PORT = 9100
WINDOWS_EXPORTER_PORT = 9182
DETECT_TIMEOUT = 1.0
METRICS_PATH = "/metrics"
AUTO_ASSET_TYPE = "Auto (detect exporter)"

MetricsFetcher = Callable[[str, float], tuple[int | None, str, str]]
SnmpProber = Callable[[str], bool]


def is_auto_asset_type(type: str = "") -> bool:
    text = (type or "").strip().lower()
    if not text:
        return False
    if text == "auto" or text.startswith("auto "):
        return True
    return "detect exporter" in text or text == "auto-detect"


def hint_kind(type: str = "", profile: str = "") -> str:
    blob = f"{type} {profile}".lower()
    if is_auto_asset_type(type):
        return ""
    if "windows" in blob or "win32" in blob:
        return "windows"
    if "linux" in blob:
        return "linux"
    if "network" in blob or "switch" in blob or "router" in blob or "firewall" in blob:
        return "network"
    return ""


def classify_exporter_metrics(text: str) -> str:
    """Return 'windows', 'linux', or '' from a Prometheus /metrics body."""
    blob = (text or "").lower()
    windows = "windows_exporter" in blob or "windows_" in blob
    linux_strong = "node_uname" in blob or "node_cpu" in blob or "node_exporter" in blob
    linux = linux_strong or "node_" in blob
    if windows and not linux:
        return "windows"
    if linux and not windows:
        return "linux"
    if windows and linux:
        if linux_strong and not ("windows_cpu" in blob or "windows_cs" in blob):
            return "linux"
        return "windows"
    return ""


def fetch_metrics(url: str, timeout: float) -> tuple[int | None, str, str]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "forgesre-detect/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096)
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


@dataclass
class ExporterDetect:
    kind: str = ""
    asset_type: str = ""
    scrape_address: str = ""
    profile: str = ""
    port: int | None = None
    linux: bool = False
    windows: bool = False
    snmp: bool = False
    linux_status: int | None = None
    windows_status: int | None = None
    linux_error: str = ""
    windows_error: str = ""
    message: str = ""
    tie_break: str = ""
    role: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _empty(ip: str, message: str) -> ExporterDetect:
    return ExporterDetect(
        message=message,
        role="",
    )


def _picked(ip: str, kind: str, message: str, tie_break: str, **ports: Any) -> ExporterDetect:
    if kind == "windows":
        port = WINDOWS_EXPORTER_PORT
        asset_type = "Windows Server"
        profile = "windows-standard"
        role = "Possible Windows server"
        scrape = f"{ip}:{port}" if ip else ""
    elif kind == "network":
        port = None
        asset_type = "Network device"
        profile = "network-switch"
        role = "Possible network device"
        scrape = ""
    else:
        port = LINUX_EXPORTER_PORT
        asset_type = "Linux Server"
        profile = "linux-standard"
        role = "Possible Linux server"
        scrape = f"{ip}:{port}" if ip else ""
    result = ExporterDetect(
        kind=kind,
        asset_type=asset_type,
        scrape_address=scrape,
        profile=profile,
        port=port,
        message=message,
        tie_break=tie_break,
        role=role,
    )
    for key, value in ports.items():
        setattr(result, key, value)
    return result


def _resolve_snmp(ip: str, snmp_ok: bool | None, snmp_prober: SnmpProber | None) -> bool:
    if snmp_ok is not None:
        return bool(snmp_ok)
    if snmp_prober is None:
        return False
    try:
        return bool(snmp_prober(ip))
    except Exception:
        return False


def detect_exporter(
    ip: str,
    hint_type: str = "",
    hint_profile: str = "",
    timeout: float = DETECT_TIMEOUT,
    fetcher: MetricsFetcher | None = None,
    snmp_ok: bool | None = None,
    snmp_prober: SnmpProber | None = None,
) -> ExporterDetect:
    ip = (ip or "").strip()
    if not ip:
        return _empty(
            "",
            "No IP — cannot detect exporter. ICMP ping is not a scrape. "
            "Pick Linux Server (:9100), Windows Server (:9182), or Network device (SNMP).",
        )
    get = fetcher or fetch_metrics
    win_url = f"http://{ip}:{WINDOWS_EXPORTER_PORT}{METRICS_PATH}"
    lnx_url = f"http://{ip}:{LINUX_EXPORTER_PORT}{METRICS_PATH}"
    win_status, win_body, win_err = get(win_url, timeout)
    lnx_status, lnx_body, lnx_err = get(lnx_url, timeout)
    win_family = classify_exporter_metrics(win_body) if win_status == 200 else ""
    lnx_family = classify_exporter_metrics(lnx_body) if lnx_status == 200 else ""
    windows = win_family == "windows"
    linux = lnx_family == "linux"
    extra = {
        "linux": linux,
        "windows": windows,
        "linux_status": lnx_status,
        "windows_status": win_status,
        "linux_error": lnx_err,
        "windows_error": win_err,
    }
    if windows and linux:
        saved = hint_kind(hint_type, hint_profile)
        if saved == "windows":
            return _picked(
                ip,
                "windows",
                f"Both exporters answered; kept Windows Server (saved type). Scrape {ip}:{WINDOWS_EXPORTER_PORT}. You can override.",
                "saved-type",
                **extra,
            )
        if saved == "linux":
            return _picked(
                ip,
                "linux",
                f"Both exporters answered; kept Linux Server (saved type). Scrape {ip}:{LINUX_EXPORTER_PORT}. You can override.",
                "saved-type",
                **extra,
            )
        return _picked(
            ip,
            "windows",
            (
                f"Both :{WINDOWS_EXPORTER_PORT} (windows_) and :{LINUX_EXPORTER_PORT} "
                f"(node_uname/node_cpu) answered; no saved type — prefer Windows Server "
                f":{WINDOWS_EXPORTER_PORT}. Override if this host is Linux."
            ),
            "windows-over-node",
            **extra,
        )
    if windows:
        return _picked(
            ip,
            "windows",
            f"Detected windows_exporter on :{WINDOWS_EXPORTER_PORT}. Type Windows Server, scrape {ip}:{WINDOWS_EXPORTER_PORT}. You can override.",
            "windows-metrics",
            **extra,
        )
    if linux:
        return _picked(
            ip,
            "linux",
            f"Detected node_exporter on :{LINUX_EXPORTER_PORT}. Type Linux Server, scrape {ip}:{LINUX_EXPORTER_PORT}. You can override.",
            "linux-metrics",
            **extra,
        )
    saved = hint_kind(hint_type, hint_profile)
    snmp = False
    if saved not in {"linux", "windows"}:
        snmp = _resolve_snmp(ip, snmp_ok, snmp_prober)
    extra["snmp"] = snmp
    if snmp:
        return _picked(
            ip,
            "network",
            (
                "SNMP UDP/161 answered (same path as snmp_exporter after Approve). "
                "Type Network device, empty scrape. Not guessed from missing :9100/:9182."
            ),
            "snmp-udp",
            **extra,
        )
    if saved == "network":
        return _picked(
            ip,
            "network",
            "Kept Network device (saved type). Polled by snmp_exporter UDP/161, not HTTP /metrics.",
            "saved-type",
            **extra,
        )
    return ExporterDetect(
        linux=False,
        windows=False,
        snmp=False,
        linux_status=lnx_status,
        windows_status=win_status,
        linux_error=lnx_err,
        windows_error=win_err,
        tie_break="none",
        message=(
            "No windows_exporter :9182/metrics, no node_exporter :9100/metrics, "
            "and no SNMP UDP/161 answer. ICMP ping is not a scrape — pick "
            "Linux Server, Windows Server, or Network device yourself, or install the exporter."
        ),
    )
