"""Live communication verify for inventory assets.

Not ``./forgesre test`` (that is appliance health). Verify is: inventory row →
ICMP / exporter or SNMP → Prometheus ``up`` → optional last RCA facts vs PromQL.
LLM is reported only when ForgeAI is enabled; verify never invents a host and
never calls the LLM itself.

Universal classes (not SKUs): Linux ``:9100`` ``node_``, Windows ``:9182``
``windows_``, Network SNMP (existing snmp_exporter path), Unknown → SKIP.
Seeded ``forge-demo-*`` rows are lab-only and are never proof of a real scrape.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from app.asset_probe import (
    LINUX_EXPORTER_PORT,
    WINDOWS_EXPORTER_PORT,
    AssetProbe,
    CheckResult,
    asset_kind,
    probe_target,
    select_assets,
)
from app.demo_ids import is_demo_asset_id
from app.exporter_detect import classify_exporter_metrics, is_auto_asset_type

PromQuery = Callable[[str], dict[str, Any]]
RCALookup = Callable[[str], dict[str, Any] | None]
SdLookup = Callable[[str], dict[str, bool]]

FACT_METRIC = re.compile(
    r"^(?P<name>[a-z][a-z0-9_]*) is (?P<value>-?[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
PERCENT_MISMATCH = 20.0
UP_MISMATCH = 0.5

CLASS_LINUX = "linux"
CLASS_WINDOWS = "windows"
CLASS_NETWORK = "network"
CLASS_UNKNOWN = "unknown"

CLASS_REASON = {
    CLASS_LINUX: "Linux — node_exporter :9100 / node_ metrics",
    CLASS_WINDOWS: "Windows — windows_exporter :9182 / windows_ metrics",
    CLASS_NETWORK: "Network — SNMP UDP/161 via snmp_exporter (not HTTP /metrics)",
    CLASS_UNKNOWN: "Unknown — not Linux :9100, Windows :9182, or Network SNMP",
}


def is_lab_asset(item: dict[str, Any] | Any) -> bool:
    if isinstance(item, dict):
        return is_demo_asset_id(item.get("asset_id") or item.get("hostname"))
    return is_demo_asset_id(getattr(item, "asset_id", "") or getattr(item, "hostname", ""))


def classify_verify(item: dict[str, Any]) -> tuple[str, str]:
    """Return (class, reason). Unknown is a first-class SKIP, not a guessed SKU."""
    kind = asset_kind(str(item.get("type") or ""), str(item.get("monitoring_profile") or ""))
    if kind == CLASS_LINUX:
        return CLASS_LINUX, CLASS_REASON[CLASS_LINUX]
    if kind == CLASS_WINDOWS:
        return CLASS_WINDOWS, CLASS_REASON[CLASS_WINDOWS]
    if kind == CLASS_NETWORK:
        return CLASS_NETWORK, CLASS_REASON[CLASS_NETWORK]
    if is_auto_asset_type(str(item.get("type") or "")):
        return (
            CLASS_UNKNOWN,
            "Type is Auto (detect exporter) with no saved Linux/Windows/Network class. "
            "SKIP until detect fills a type — missing :9100/:9182 is not a fingerprint.",
        )
    if kind == "web":
        return (
            CLASS_UNKNOWN,
            "Web/appliance is inventory-only until you set Linux :9100, Windows :9182, "
            "or Network SNMP. No exporter class to verify.",
        )
    return CLASS_UNKNOWN, CLASS_REASON[CLASS_UNKNOWN]


def _check(name: str, ok: bool | None, detail: str) -> CheckResult:
    return CheckResult(name, ok, detail)


def family_check(vclass: str, probe: AssetProbe) -> CheckResult:
    if vclass == CLASS_UNKNOWN:
        return _check("family", None, "no exporter class — not node_ / windows_ / SNMP")
    if vclass == CLASS_NETWORK:
        if probe.metrics.ok is True:
            return _check("family", True, "SNMP if_mib (snmp_exporter UDP/161)")
        if probe.metrics.ok is False:
            return _check("family", False, "SNMP UDP/161 did not answer — no if_mib walk")
        return _check("family", None, "SNMP not probed (no IP)")
    preview = probe.metrics.preview or ""
    if extra := probe.extra:
        if extra[0].ok is True and extra[0].preview:
            preview = extra[0].preview
    family = classify_exporter_metrics(preview) if preview else ""
    want = "linux" if vclass == CLASS_LINUX else "windows"
    token = "node_" if want == "linux" else "windows_"
    port = LINUX_EXPORTER_PORT if want == "linux" else WINDOWS_EXPORTER_PORT
    if probe.metrics.ok is not True and not (probe.extra and probe.extra[0].ok is True):
        if probe.metrics.ok is False:
            return _check(
                "family",
                None,
                f"no /metrics body on :{port} — cannot confirm {token} family",
            )
        return _check("family", None, f"no scrape port for {token} family")
    if family == want:
        return _check("family", True, f"{token} family on :{port}")
    if family:
        other = "windows_" if family == "windows" else "node_"
        return _check(
            "family",
            False,
            f"expected {token} on :{port}, got {other} — type/scrape mismatch",
        )
    if probe.metrics.ok is True:
        return _check(
            "family",
            False,
            f"HTTP 200 on :{port} but not {token} metrics — not a fake PASS",
        )
    return _check("family", None, f"no {token} metrics")


def prometheus_check(
    item: dict[str, Any],
    vclass: str,
    *,
    lab: bool,
    in_http_sd: bool,
    in_snmp_sd: bool,
    query_fn: PromQuery | None,
) -> CheckResult:
    asset_id = str(item.get("asset_id") or "")
    scrape = str(item.get("scrape_address") or "").strip()
    if lab:
        return _check(
            "prom",
            None,
            "DEMO lab host (forge-demo-*) is not in HTTP/SNMP SD — not proof of a real scrape",
        )
    if vclass == CLASS_UNKNOWN:
        return _check("prom", None, "no exporter class — Prometheus has nothing to scrape")
    if vclass == CLASS_NETWORK:
        if not in_snmp_sd:
            return _check(
                "prom",
                None,
                "not in SNMP HTTP SD (need type Network device + IP). Prometheus cannot see it",
            )
        expr = f'up{{job="forgesre-snmp",asset="{asset_id}"}}'
    else:
        if not in_http_sd:
            why = "empty scrape_address" if not scrape else "not listed in Prometheus HTTP SD"
            return _check(
                "prom",
                None,
                f"{why} — Prometheus has no target. Missing exporter is SKIP, not a fake host",
            )
        expr = f'up{{asset="{asset_id}"}}'
    if query_fn is None:
        return _check("prom", None, f"Prometheus not queried ({expr})")
    sample = query_fn(expr)
    if sample.get("error"):
        return _check(
            "prom",
            None,
            f"Prometheus unreachable ({expr}): {sample['error']}",
        )
    value = sample.get("value")
    if value is None:
        return _check(
            "prom",
            None,
            f"no up sample yet for {expr} (wait a scrape interval, then ./forgesre sd). Not PASS",
        )
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _check("prom", None, f"up returned {value!r} for {expr}")
    if number >= 1:
        return _check("prom", True, f"Prometheus up=1 ({expr})")
    return _check("prom", False, f"Prometheus up={number:g} ({expr}) — target listed, scrape failed")


def parse_fact_metrics(facts: list[dict[str, Any]] | None) -> dict[str, float]:
    found: dict[str, float] = {}
    for fact in facts or []:
        text = str(fact.get("text") or "")
        match = FACT_METRIC.match(text.strip())
        if not match:
            continue
        try:
            found[match.group("name")] = float(match.group("value"))
        except (TypeError, ValueError):
            continue
    return found


def rca_check(
    item: dict[str, Any],
    *,
    rca: dict[str, Any] | None,
    live_metrics: dict[str, Any] | None,
) -> CheckResult:
    del item
    if not rca:
        return _check("rca", None, "no RCA investigation for this asset")
    number = rca.get("incident") or rca.get("incident_id") or ""
    facts = rca.get("facts") or []
    stored = parse_fact_metrics(facts)
    live = live_metrics or {}
    mismatches: list[str] = []
    for name, old in stored.items():
        if name not in live or live.get(name) is None:
            continue
        try:
            now = float(live[name])
        except (TypeError, ValueError):
            continue
        limit = UP_MISMATCH if name == "up" else PERCENT_MISMATCH
        if abs(now - old) > limit:
            mismatches.append(f"{name} RCA={old:g} PromQL={now:g}")
    if mismatches:
        return _check(
            "rca",
            False,
            f"last RCA {number} mismatches live PromQL: " + "; ".join(mismatches),
        )
    if stored:
        return _check("rca", True, f"last RCA {number} facts still match live PromQL")
    summary = (rca.get("summary") or rca.get("likely_cause") or "recorded").strip()
    return _check("rca", True, f"last RCA {number}: {summary[:160]}")


def llm_check(*, ai_enabled: bool, rca: dict[str, Any] | None) -> CheckResult:
    if not ai_enabled:
        return _check("llm", None, "ForgeAI disabled — LLM not called")
    provider = str((rca or {}).get("provider") or "")
    if provider == "forgerca-llm":
        return _check("llm", True, "ForgeAI enabled; last RCA prose was rewritten (read-only)")
    return _check(
        "llm",
        None,
        "ForgeAI enabled; last RCA is ForgeRCA builtin only. Verify does not invoke the LLM",
    )


def port_check(vclass: str, probe: AssetProbe) -> CheckResult:
    if vclass == CLASS_UNKNOWN:
        detail = probe.metrics.detail or "no exporter class"
        return _check("port", None, detail)
    return CheckResult(
        "port",
        probe.metrics.ok,
        probe.metrics.detail,
        probe.metrics.elapsed_ms,
        preview=probe.metrics.preview,
    )


def overall_status(
    *,
    lab: bool,
    vclass: str,
    metrics: CheckResult,
    family: CheckResult,
    prom: CheckResult,
) -> tuple[str, str]:
    if lab:
        return (
            "SKIP",
            "DEMO lab host (forge-demo-*) — labeled lab, not proof of a real scrape",
        )
    if vclass == CLASS_UNKNOWN:
        return "SKIP", CLASS_REASON[CLASS_UNKNOWN]
    if metrics.ok is False or family.ok is False or prom.ok is False:
        bits = []
        if metrics.ok is False:
            bits.append(metrics.detail or "exporter/SNMP failed")
        if family.ok is False:
            bits.append(family.detail or "metric family mismatch")
        if prom.ok is False:
            bits.append(prom.detail or "Prometheus up=0")
        return "FAIL", "; ".join(bits)
    if metrics.ok is True and family.ok is True and prom.ok is True:
        return "PASS", "ICMP/exporter path plus Prometheus up=1"
    if metrics.ok is True and family.ok is True and prom.ok is None:
        return "SKIP", prom.detail or "exporter answers; Prometheus does not see it yet"
    if metrics.ok is None:
        return "SKIP", metrics.detail or "no exporter / no Prom target"
    return "SKIP", prom.detail or family.detail or metrics.detail or "incomplete live path"


@dataclass
class VerifyReport:
    asset_id: str
    hostname: str
    ip: str
    type: str
    scrape: str
    environment: str
    owner: str
    contact_name: str
    owner_email: str
    owner_phone: str
    notes: str
    source: str
    profile: str
    status: str
    vclass: str
    class_reason: str
    lab: bool
    inventory: dict[str, Any]
    icmp: CheckResult
    port: CheckResult
    family: CheckResult
    prom: CheckResult
    rca: CheckResult
    llm: CheckResult
    overall: str
    overall_reason: str
    hint: str = ""
    extra: list[CheckResult] = field(default_factory=list)
    probe: AssetProbe | None = None

    def as_dict(self) -> dict[str, Any]:
        def pack(check: CheckResult) -> dict[str, Any]:
            return {"mark": check.mark, "ok": check.ok, "detail": check.detail}

        return {
            "asset_id": self.asset_id,
            "hostname": self.hostname,
            "ip": self.ip,
            "type": self.type,
            "scrape_address": self.scrape,
            "environment": self.environment,
            "owner": self.owner,
            "contact_name": self.contact_name,
            "owner_email": self.owner_email,
            "owner_phone": self.owner_phone,
            "notes": self.notes,
            "source": self.source,
            "monitoring_profile": self.profile,
            "status": self.status,
            "class": self.vclass,
            "class_reason": self.class_reason,
            "lab": self.lab,
            "demo": self.lab,
            "inventory": self.inventory,
            "icmp": pack(self.icmp),
            "port": pack(self.port),
            "family": pack(self.family),
            "prom": pack(self.prom),
            "rca": pack(self.rca),
            "llm": pack(self.llm),
            "overall": self.overall,
            "overall_reason": self.overall_reason,
            "hint": self.hint,
            "extra": [pack(item) for item in self.extra],
        }


def inventory_dump(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "asset_id",
        "hostname",
        "ip",
        "type",
        "environment",
        "status",
        "monitoring_profile",
        "owner",
        "contact_name",
        "owner_email",
        "owner_phone",
        "notes",
        "source",
        "scrape_address",
        "snmp",
        "ping",
        "ping_detail",
        "exporter",
        "exporter_detail",
        "exporter_label",
        "probe_checked_at",
    )
    return {key: item.get(key, "") for key in keys}


def compose_verify(
    item: dict[str, Any],
    probe: AssetProbe,
    *,
    in_http_sd: bool = False,
    in_snmp_sd: bool = False,
    query_fn: PromQuery | None = None,
    rca: dict[str, Any] | None = None,
    live_metrics: dict[str, Any] | None = None,
    ai_enabled: bool = False,
) -> VerifyReport:
    vclass, class_reason = classify_verify(item)
    lab = is_lab_asset(item)
    icmp = probe.icmp
    port = port_check(vclass, probe)
    family = family_check(vclass, probe)
    prom = prometheus_check(
        item,
        vclass,
        lab=lab,
        in_http_sd=in_http_sd,
        in_snmp_sd=in_snmp_sd,
        query_fn=query_fn,
    )
    rca_result = rca_check(item, rca=rca, live_metrics=live_metrics)
    llm = llm_check(ai_enabled=ai_enabled, rca=rca)
    overall, reason = overall_status(lab=lab, vclass=vclass, metrics=port, family=family, prom=prom)
    return VerifyReport(
        asset_id=str(item.get("asset_id") or probe.asset_id),
        hostname=str(item.get("hostname") or probe.hostname or ""),
        ip=str(item.get("ip") or probe.ip or ""),
        type=str(item.get("type") or ""),
        scrape=str(item.get("scrape_address") or probe.scrape or ""),
        environment=str(item.get("environment") or ""),
        owner=str(item.get("owner") or ""),
        contact_name=str(item.get("contact_name") or ""),
        owner_email=str(item.get("owner_email") or ""),
        owner_phone=str(item.get("owner_phone") or ""),
        notes=str(item.get("notes") or ""),
        source=str(item.get("source") or ""),
        profile=str(item.get("monitoring_profile") or ""),
        status=str(item.get("status") or ""),
        vclass=vclass,
        class_reason=class_reason,
        lab=lab,
        inventory=inventory_dump(item),
        icmp=icmp,
        port=port,
        family=family,
        prom=prom,
        rca=rca_result,
        llm=llm,
        overall=overall,
        overall_reason=reason,
        hint=probe.hint,
        extra=list(probe.extra or []),
        probe=probe,
    )


def verify_target(
    item: dict[str, Any],
    *,
    timeout: float = 2.0,
    ping_runner=None,
    metrics_fetcher=None,
    snmp_prober=None,
    in_http_sd: bool = False,
    in_snmp_sd: bool = False,
    query_fn: PromQuery | None = None,
    rca: dict[str, Any] | None = None,
    live_metrics: dict[str, Any] | None = None,
    ai_enabled: bool = False,
    probe: AssetProbe | None = None,
) -> VerifyReport:
    if probe is None:
        probe = probe_target(
            item,
            timeout=timeout,
            ping_runner=ping_runner,
            metrics_fetcher=metrics_fetcher,
            snmp_prober=snmp_prober,
        )
    return compose_verify(
        item,
        probe,
        in_http_sd=in_http_sd,
        in_snmp_sd=in_snmp_sd,
        query_fn=query_fn,
        rca=rca,
        live_metrics=live_metrics,
        ai_enabled=ai_enabled,
    )


def urllib_prom_query(expr: str, base_url: str = "http://127.0.0.1:9090", timeout: float = 5.0) -> dict[str, Any]:
    """GET Prometheus /api/v1/query. Stdlib only — host CLI has no httpx requirement."""
    root = (base_url or "http://127.0.0.1:9090").rstrip("/")
    url = f"{root}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "forgesre-verify/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "query": expr}
    except Exception as exc:  # noqa: BLE001 — live probe
        return {"error": str(exc), "query": expr}
    result = ((payload.get("data") or {}).get("result")) or []
    if result:
        try:
            return {"value": float(result[0]["value"][1]), "query": expr}
        except (KeyError, IndexError, TypeError, ValueError):
            return {"value": None, "query": expr}
    return {"value": None, "query": expr}


def verify_exit(results: list[VerifyReport]) -> int:
    if any(row.overall == "FAIL" for row in results):
        return 1
    return 0


def _mark(check: CheckResult, color: bool) -> str:
    from app.cli_view import DIM, GREEN, RED, paint

    code = GREEN if check.ok is True else (RED if check.ok is False else DIM)
    return paint(f"{check.mark:<6}", code, color)


def format_verify_one(report: VerifyReport, *, color: bool = False) -> str:
    from app.cli_view import BOLD, DIM, GREEN, RED, YELLOW, paint

    overall_color = {"PASS": GREEN, "FAIL": RED, "SKIP": DIM, "WARN": YELLOW}.get(report.overall, "")
    lab = "  DEMO lab" if report.lab else ""
    lines = [
        paint(f"ForgeSRE verify{lab}  {report.asset_id}", BOLD, color),
        "Live communication (inventory → ICMP/exporter or SNMP → Prometheus). Not ./forgesre test.",
        "",
        paint("=== Inventory ===", BOLD, color),
        f"asset_id     {report.asset_id}",
        f"hostname     {report.hostname or '—'}",
        f"ip           {report.ip or '—'}",
        f"type         {report.type or '—'}",
        f"profile      {report.profile or '—'}",
        f"scrape       {report.scrape or '—'}",
        f"environment  {report.environment or '—'}",
        f"status       {report.status or '—'}",
        f"owner        {report.owner or '—'}",
        f"contact      {report.contact_name or '—'}",
        f"email        {report.owner_email or '—'}",
        f"phone        {report.owner_phone or '—'}",
        f"source       {report.source or '—'}",
        f"notes        {report.notes or '—'}",
        "",
        paint("=== Class ===", BOLD, color),
        f"{report.vclass:<12} {report.class_reason}",
        "",
        paint("=== Live probes ===", BOLD, color),
        f"ICMP     {_mark(report.icmp, color)} {report.icmp.detail}",
        f"PORT     {_mark(report.port, color)} {report.port.detail}",
        f"FAMILY   {_mark(report.family, color)} {report.family.detail}",
        f"PROM     {_mark(report.prom, color)} {report.prom.detail}",
        f"RCA      {_mark(report.rca, color)} {report.rca.detail}",
        f"LLM      {_mark(report.llm, color)} {report.llm.detail}",
    ]
    for extra in report.extra:
        lines.append(f"         {_mark(extra, color)} also {extra.detail}")
    if report.hint:
        lines.append("")
        lines.append(report.hint)
    lines.append("")
    lines.append(paint("=== Overall ===", BOLD, color))
    lines.append(paint(f"{report.overall}  {report.overall_reason}", overall_color + BOLD, color))
    if report.lab:
        lines.append(paint("Lab DEMO — do not treat this row as a real scrape.", DIM, color))
    return "\n".join(lines) + "\n"


def format_verify_many(
    results: list[VerifyReport],
    *,
    skipped_demo: int = 0,
    color: bool = False,
) -> str:
    from app.cli_view import BOLD, DIM, GREEN, RED, YELLOW, paint

    lines = [
        paint("ForgeSRE verify", BOLD, color),
        "Live communication — not ./forgesre test (that is appliance health after update).",
        "Classes: Linux :9100 node_ · Windows :9182 windows_ · Network SNMP · Unknown SKIP.",
        "",
        f"{'ASSET':<22} {'CLASS':<10} {'ICMP':<6} {'PORT':<6} {'PROM':<6} {'FAMILY':<6} {'RCA':<6} {'LLM'}",
        "-" * 88,
    ]
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "WARN": 0}
    for row in results:
        counts[row.overall] = counts.get(row.overall, 0) + 1
        name = ("DEMO " + row.asset_id) if row.lab else row.asset_id
        lines.append(
            f"{name[:22]:<22} {row.vclass:<10} "
            f"{_mark(row.icmp, color)} {_mark(row.port, color)} {_mark(row.prom, color)} "
            f"{_mark(row.family, color)} {_mark(row.rca, color)} {_mark(row.llm, color)}  {row.overall_reason}"
        )
    lines.append("")
    summary = f"{counts['PASS']} PASS  {counts['FAIL']} FAIL  {counts['SKIP']} SKIP"
    lines.append(paint(summary, RED if counts["FAIL"] else GREEN, color))
    if skipped_demo:
        lines.append(
            paint(
                f"skipped {skipped_demo} demo lab asset(s). Include labeled DEMO with: ./forgesre verify --demo",
                DIM,
                color,
            )
        )
    return "\n".join(lines) + "\n"


def format_verify_report(
    results: list[VerifyReport],
    *,
    skipped_demo: int = 0,
    color: bool = False,
    detail: bool = False,
) -> str:
    if detail or len(results) == 1:
        chunks = [format_verify_one(row, color=color) for row in results]
        if skipped_demo:
            from app.cli_view import DIM, paint

            chunks.append(
                paint(
                    f"skipped {skipped_demo} demo lab asset(s). Include labeled DEMO with: ./forgesre verify --demo",
                    DIM,
                    color,
                )
                + "\n"
            )
        return "".join(chunks)
    return format_verify_many(results, skipped_demo=skipped_demo, color=color)


__all__ = [
    "CLASS_LINUX",
    "CLASS_NETWORK",
    "CLASS_UNKNOWN",
    "CLASS_WINDOWS",
    "VerifyReport",
    "classify_verify",
    "compose_verify",
    "format_verify_report",
    "is_lab_asset",
    "parse_fact_metrics",
    "select_assets",
    "verify_exit",
    "verify_target",
]
