"""SNMP: device → snmp_exporter → Prometheus → ForgeSRE, block by block (registered in app.flows).

Whole flow: SNMP assets in the inventory → their auths in snmp.yml → snmp_exporter → the
Prometheus SNMP jobs (targets up, last errors per device). One device (``target`` = asset id, opened
from the asset page): the asset's SNMP settings → its auth in snmp.yml → an SNMP GET of sysDescr from
ForgeSRE (v1/v2c) → snmp_exporter walking it → its Prometheus target → series stored. All read-only;
community strings and v3 passwords are used for the GET but never shown, logged or returned.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.flows import Flow, network_steps, register, step
from app.settings import settings

EXPORTER_TIMEOUT = 10.0
JOB_PREFIX = "forgesre-snmp"


def exporter_url() -> str:
    return settings.snmp_exporter_url


def _snmp_assets(db: Session) -> list[Any]:
    from app.inventory import is_snmp_asset
    from app.models import Asset

    return [a for a in db.query(Asset).order_by(Asset.asset_id).all() if is_snmp_asset(a)]


def _snmp_targets() -> tuple[list[dict[str, Any]], int, str]:
    """Active Prometheus targets of the forgesre-snmp* jobs: (targets, ms, error)."""
    from app.flow_prometheus import _get, prom_url

    data, ms, err = _get(prom_url(), "/api/v1/targets", {"state": "active"})
    if err or not isinstance(data, dict):
        return [], ms, err or "no answer"
    rows = (data.get("data") or {}).get("activeTargets") or []
    return [t for t in rows if str(t.get("scrapePool", "")).startswith(JOB_PREFIX)], ms, ""


def _target_row(t: dict[str, Any]) -> dict[str, Any]:
    labels = t.get("labels") or {}
    return {"at": t.get("lastScrape", ""), "status": t.get("health", ""), "error": (t.get("lastError") or "")[:160],
            "subject": f"{labels.get('asset') or labels.get('instance', '')} · {t.get('scrapePool', '')}"}


def _auth_step(db: Session, assets: list[Any]) -> dict[str, Any]:
    from app.asset_snmp import auth_status, generated_snmp_path
    from app.snmp_apply import BEGIN, END

    path = generated_snmp_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return step("auths", "Auths", "snmp.yml", "bad", f"{path} is missing, so snmp_exporter has no auths.",
                    fix="./forgesre render-monitoring")
    if BEGIN not in text or END not in text:
        return step("auths", "Auths", "snmp.yml", "bad", "snmp.yml has no forgesre-asset-auths markers.", fix="./forgesre render-monitoring")
    pending = [a for a in assets if auth_status(a).get("pending")]
    rows = [{"at": "", "status": "pending", "error": f"wants {auth_status(a)['desired']}, uses {auth_status(a)['effective']}",
             "subject": a.hostname} for a in pending[:15]]
    return step("auths", "Auths", "snmp.yml", "warn" if pending else "ok",
                f"{len(pending)} device(s) wait for their own auth in snmp.yml; they are walked with public_v2 meanwhile." if pending
                else "Every device's community or v3 user is in snmp.yml.",
                rows=rows, fix="./forgesre snmp-auths (or save the asset again: it queues the apply job)" if pending else "")


def _exporter_steps() -> list[dict[str, Any]]:
    url = exporter_url()
    net, ok = network_steps(url, what="snmp_exporter", prefix="exp_")
    if not ok:
        return net
    started = time.monotonic()
    try:
        r = httpx.get(f"{url}/metrics", timeout=3.0)
        ms = int((time.monotonic() - started) * 1000)
        good = r.status_code < 300
        net.append(step("exporter", "snmp_exporter", "/metrics", "ok" if good else "bad",
                        "snmp_exporter answers." if good else f"snmp_exporter answered HTTP {r.status_code}.", ms=ms,
                        fix="" if good else "docker compose logs snmp-exporter && docker compose up -d snmp-exporter"))
    except httpx.HTTPError as exc:
        net.append(step("exporter", "snmp_exporter", "/metrics", "bad", f"snmp_exporter does not answer: {exc.__class__.__name__}.",
                        fix="docker compose up -d snmp-exporter"))
    return net


def check(db: Session) -> dict[str, Any]:
    """Every SNMP device: inventory → auths → snmp_exporter → Prometheus SNMP jobs."""
    assets = _snmp_assets(db)
    steps = [step("inventory", "Inventory", f"{len(assets)} SNMP device(s)", "ok" if assets else "unknown",
                  f"{len(assets)} device(s) are polled over SNMP." if assets else "No asset is polled over SNMP yet (network types, or SNMP poll on).")]
    steps.append(_auth_step(db, assets))
    steps += _exporter_steps()
    targets, ms, err = _snmp_targets()
    if err:
        steps.append(step("targets", "Prometheus", "SNMP jobs", "bad", f"Prometheus does not answer: {err}.", ms=ms,
                          fix="docker compose up -d prometheus"))
    else:
        down = [t for t in targets if t.get("health") != "up"]
        rows = [_target_row(t) for t in down[:15]] + [_target_row(t) for t in targets if t.get("health") == "up"][: max(0, 15 - len(down))]
        status = "unknown" if not targets else ("ok" if not down else ("warn" if len(down) < len(targets) else "bad"))
        steps.append(step("targets", "Prometheus", f"{len(targets)} SNMP target(s)", status,
                          (f"{len(targets) - len(down)} of {len(targets)} walked; {len(down)} failing." if targets
                           else "Prometheus has no SNMP target (the SNMP SD lists none, or the job is missing)."),
                          ms=ms, rows=rows, fix="Open the failing device (Inventory › asset › SNMP flow) to see where its walk breaks." if down else ""))
    return {"lanes": [{"id": "in", "title": "Devices → snmp_exporter → Prometheus", "steps": steps}]}


# --- One device ---------------------------------------------------------------------------------

def _asset(db: Session, target: str) -> Any:
    from app.models import Asset

    asset = db.get(Asset, int(target)) if target.isdigit() else None
    return asset if asset is not None else db.query(Asset).filter(Asset.asset_id == target).first()


def target_label(db: Session, target: str) -> str:
    asset = _asset(db, target)
    return f"{asset.hostname} ({asset.ip})" if asset is not None else target


def probe_exporter(ip_target: str, module: str, auth: str) -> tuple[str, int, str]:
    """Ask snmp_exporter to walk one device now: (status ok|bad, series, error). Read-only SNMP."""
    try:
        r = httpx.get(f"{exporter_url()}/snmp", params={"target": ip_target, "module": module, "auth": auth}, timeout=EXPORTER_TIMEOUT)
    except httpx.HTTPError as exc:
        return "bad", 0, f"snmp_exporter does not answer: {exc.__class__.__name__}"
    if r.status_code >= 300:
        return "bad", 0, (r.text.strip().splitlines() or [f"HTTP {r.status_code}"])[-1][:240]
    series = sum(1 for line in r.text.splitlines() if line and not line.startswith("#") and not line.startswith("snmp_scrape"))
    return "ok", series, ""


def check_device(db: Session, target: str) -> dict[str, Any]:
    """One device: settings → auth → SNMP GET from ForgeSRE → exporter walk → Prometheus target."""
    from app.asset_snmp import auth_status
    from app.asset_types import snmp_port_for, snmp_target
    from app.inventory import is_snmp_asset
    from app.snmp_get import system_info
    from app.snmp_onboard import community_for_asset

    asset = _asset(db, target)
    if asset is None:
        return {"lanes": [{"id": "in", "title": "Device", "steps": [step("asset", "Asset", target, "bad", "No such asset.")]}]}
    port = snmp_port_for(asset)
    auth = auth_status(asset)
    version = auth.get("snmp_version") or "v2c"
    steps = [step("asset", "Asset", asset.hostname, "ok" if is_snmp_asset(asset) else "bad",
                  f"{asset.ip or 'no IP'}, UDP {port or '—'}, SNMP {version}." if is_snmp_asset(asset)
                  else "This asset is not polled over SNMP (no IP, SNMP off, or a type without SNMP).",
                  fix="" if is_snmp_asset(asset) else "Asset › Edit › Monitoring: turn SNMP poll on and give it an IP.")]
    if not is_snmp_asset(asset):
        return {"lanes": [{"id": "in", "title": f"{asset.hostname} → snmp_exporter → Prometheus", "steps": steps}]}
    steps.append(step("auth", "Auth", auth["effective"], "warn" if auth.get("pending") else "ok",
                      f"Wants {auth['desired']}, snmp_exporter still uses {auth['effective']}." if auth.get("pending")
                      else f"snmp_exporter uses auth {auth['effective']}.", fix="./forgesre snmp-auths" if auth.get("pending") else ""))
    if version in {"v1", "v2c"}:
        started = time.monotonic()
        info = system_info(asset.ip, community_for_asset(asset), version=version, port=port, timeout=2.0)
        ms = int((time.monotonic() - started) * 1000)
        if info.get("error"):
            steps.append(step("get", "SNMP GET", f"{asset.ip}:{port}", "bad", f"From ForgeSRE: {info['error']}.", ms=ms,
                              fix=f"Check the device's SNMP ACL allows this server, the community and the version. From the server: snmpget -{version} -c <community> {asset.ip} 1.3.6.1.2.1.1.1.0"))
        else:
            steps.append(step("get", "SNMP GET", f"{asset.ip}:{port}", "ok", f"Answers: {(info.get('sysDescr') or info.get('sysName') or 'sysDescr empty')[:120]}.", ms=ms))
    else:
        steps.append(step("get", "SNMP GET", f"{asset.ip}:{port}", "unknown", "SNMP v3: ForgeSRE does not test v3 itself; the exporter walk below does."))
    module = settings.snmp_module
    started = time.monotonic()
    status, series, err = probe_exporter(snmp_target(asset.ip, port), module, auth["effective"])
    ms = int((time.monotonic() - started) * 1000)
    steps.append(step("walk", "snmp_exporter", f"module {module}", status if status == "bad" or series else "warn",
                      f"Walk failed: {err}." if status == "bad" else (f"Walked: {series} series." if series else "Walked, but the module returned no series."),
                      ms=ms, fix="A timeout: ACL, community or v3 user on the device. 'unknown auth': ./forgesre snmp-auths." if status == "bad" else ""))
    targets, tms, terr = _snmp_targets()
    mine = [t for t in targets if (t.get("labels") or {}).get("asset") == asset.asset_id]
    if terr:
        steps.append(step("target", "Prometheus", "target", "bad", f"Prometheus does not answer: {terr}.", ms=tms))
    elif not mine:
        steps.append(step("target", "Prometheus", "target", "bad", "Prometheus has no SNMP target for this asset.",
                          fix="Wait one SD refresh (60 s); if it stays missing, check the asset's IP and SNMP settings."))
    else:
        down = [t for t in mine if t.get("health") != "up"]
        steps.append(step("target", "Prometheus", f"{len(mine)} job(s)", "bad" if down else "ok",
                          f"Failing: {down[0].get('lastError') or down[0].get('health')}." if down else "Every SNMP job scrapes it.",
                          rows=[_target_row(t) for t in mine], ms=tms))
    return {"lanes": [{"id": "in", "title": f"{asset.hostname} → snmp_exporter → Prometheus", "steps": steps}]}


register(Flow(
    name="snmp",
    label="SNMP",
    check=check,
    configured=lambda: bool(settings.snmp_enabled),
    log_words=("snmp",),
    services=("core", "snmp-exporter"),
    about="How a network device's counters reach ForgeSRE: inventory, auths in snmp.yml, snmp_exporter and the Prometheus SNMP jobs. "
          "Open one device from its asset page.",
    target_check=check_device,
    target_label=target_label,
))
