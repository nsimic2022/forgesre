"""Core-side Zabbix work: host import, health journal, backup problem poll.

Read-only toward Zabbix (every call goes through app.zabbix.call, which only
allows ``*.get``). Runs inside Core: Discovery button, the 6 h discovery loop
(when inventory.zabbix.auto_sync is true) and the existing jobs loop for the
throttled poll. No sidecar container.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.demo_ids import is_lab_inventory_row
from app.exporter_detect import AUTO_ASSET_TYPE
from app.journal import report
from app.models import Asset, Incident, JournalEntry, utcnow
from app.settings import settings
from app.zabbix import (
    ZabbixError,
    ZabbixUnavailable,
    active_problem_triggerids,
    agent_state,
    call,
    list_hosts,
    server_version,
    zabbix_status,
)

log = logging.getLogger("forgesre")

PROBLEM_POLL_SECONDS = 180.0
AGENT_POLL_SECONDS = 300.0
RESOLVE_GRACE = timedelta(minutes=2)
AGENT_STATES = ("up", "down", "unknown")
# Zabbix default host groups say nothing about customer or site.
GENERIC_GROUPS = {
    "templates",
    "discovered hosts",
    "zabbix servers",
    "linux servers",
    "windows servers",
    "virtual machines",
    "hypervisors",
    "applications",
    "databases",
}
_poll_state: dict[str, float] = {"problems": 0.0, "agents": 0.0}


def _loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in {"::1", "0.0.0.0"}


def zabbix_asset_id(host: str, hostid: str = "") -> str:
    """ForgeSRE asset_id from a Zabbix technical host name (lowercase, a-z0-9-, ≤63)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (host or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:63].strip("-")
    if not slug:
        slug = f"zbx-{re.sub(r'[^a-z0-9]', '', str(hostid).lower()) or 'host'}"
    return slug


def _unique_asset_id(db: Session, base: str) -> str:
    candidate = base
    n = 2
    while db.query(Asset.id).filter(Asset.asset_id == candidate).first() is not None:
        suffix = f"-{n}"
        candidate = f"{base[: 63 - len(suffix)].rstrip('-')}{suffix}"
        n += 1
    return candidate


def group_extras(groups: list[str]) -> dict[str, str]:
    """First non-default host group → customer (and site for nested ``Customer/Site``)."""
    for name in groups or []:
        text = str(name or "").strip()
        if not text or text.lower() in GENERIC_GROUPS or text.lower().startswith("templates"):
            continue
        parts = [part.strip() for part in text.split("/") if part.strip()]
        if not parts:
            continue
        out = {"customer": parts[0][:255]}
        if len(parts) > 1:
            out["site"] = parts[1][:255]
        return out
    return {}


def zabbix_notes(host: dict[str, Any]) -> str:
    """Snapshot for a newly imported asset. Never rewritten on later syncs."""
    parts = []
    if host.get("name") and host.get("name") != host.get("host"):
        parts.append(f"visible name={host['name']}")
    if host.get("dns"):
        parts.append(f"dns={host['dns']}")
    groups = [str(item) for item in host.get("groups") or [] if str(item).strip()]
    if groups:
        parts.append(f"groups={', '.join(groups)}")
    head = f"Imported from Zabbix ({'; '.join(parts)})." if parts else "Imported from Zabbix."
    return f"{head} Type is Auto and nothing is scraped yet — use Detect or Edit to pick the exporter."


def _fill_extras(asset: Asset, groups: list[str]) -> bool:
    proposed = group_extras(groups)
    if not proposed:
        return False
    current = dict(asset.extras or {}) if isinstance(asset.extras, dict) else {}
    changed = False
    for key, value in proposed.items():
        if not str(current.get(key) or "").strip():
            current[key] = value
            changed = True
    if changed:
        asset.extras = current
    return changed


def sync_zabbix(db: Session, *, force: bool = False) -> dict[str, Any]:
    """host.get → assets. Never writes to Zabbix.

    New host: type Auto, empty scrape, source=zabbix, zabbix_hostid set, no owner.
    Existing asset (matched by zabbix_hostid, then IP, then hostname): only
    zabbix_hostid, an empty IP, empty customer/site extras and the agent pill are
    filled — owner, contact, email, phone, notes and other extras are never touched.
    """
    if not settings.zabbix_enabled:
        return {"synced": 0, "skipped": True}
    started = time.monotonic()
    try:
        hosts = list_hosts(settings.zabbix_url, settings.zabbix_token, timeout=settings.zabbix_timeout, force=force)
    except ZabbixError as exc:
        detail = str(exc)
        summary = "Zabbix sync failed: unreachable" if isinstance(exc, ZabbixUnavailable) else "Zabbix sync failed"
        log.warning("zabbix sync failed: %s", detail)
        last = (
            db.query(JournalEntry).filter_by(module="zabbix", action="sync").order_by(JournalEntry.id.desc()).first()
        )
        if not (last is not None and last.status == "error" and last.summary == summary and last.detail == detail):
            report(db, "zabbix", "sync", "error", summary=summary, detail=detail)
        return {"synced": 0, "error": detail}

    rows = db.query(Asset).order_by(Asset.id).all()
    by_hostid: dict[str, Asset] = {}
    by_ip: dict[str, Asset] = {}
    by_name: dict[str, Asset] = {}
    for asset in rows:
        if is_lab_inventory_row(asset):
            continue
        if (asset.zabbix_hostid or "").strip():
            by_hostid.setdefault(asset.zabbix_hostid.strip(), asset)
        ip = (asset.ip or "").strip()
        if ip and not _loopback(ip):
            by_ip.setdefault(ip, asset)
        for key in (asset.hostname, asset.asset_id):
            if (key or "").strip():
                by_name.setdefault(key.strip().lower(), asset)

    created = linked = skipped = 0
    for host in hosts:
        hostid = host["hostid"]
        ip = str(host.get("ip") or "").strip()
        technical = str(host.get("host") or "").strip()
        asset = by_hostid.get(hostid)
        if asset is None and ip and not _loopback(ip):
            asset = by_ip.get(ip)
        if asset is None:
            for key in (technical, host.get("name"), zabbix_asset_id(technical, hostid)):
                asset = by_name.get(str(key or "").strip().lower())
                if asset is not None:
                    break
        if asset is not None and (asset.zabbix_hostid or "").strip() and asset.zabbix_hostid != hostid:
            skipped += 1
            continue
        if asset is None:
            asset = Asset(
                asset_id=_unique_asset_id(db, zabbix_asset_id(technical, hostid)),
                hostname=technical,
                ip=ip,
                type=AUTO_ASSET_TYPE,
                environment="Production",
                status="healthy" if host.get("monitored", True) else "offline",
                monitoring_profile="",
                owner="",
                source="zabbix",
                zabbix_hostid=hostid,
                zabbix_agent=host.get("agent") or "unknown",
                scrape_address="",
                notes=zabbix_notes(host),
                extras=group_extras(host.get("groups") or []),
            )
            db.add(asset)
            db.flush()
            created += 1
        else:
            if not (asset.zabbix_hostid or "").strip():
                asset.zabbix_hostid = hostid
            if ip and not (asset.ip or "").strip():
                asset.ip = ip
            if not (asset.source or "").strip():
                asset.source = "zabbix"
            _fill_extras(asset, host.get("groups") or [])
            asset.zabbix_agent = host.get("agent") or "unknown"
            linked += 1
        by_hostid[hostid] = asset
        if ip and not _loopback(ip):
            by_ip.setdefault(ip, asset)
        by_name.setdefault(technical.lower(), asset)
    db.commit()
    _poll_state["agents"] = time.monotonic()
    report(
        db,
        "zabbix",
        "sync",
        "ok",
        summary=f"Zabbix sync: {created} new, {linked} linked, {skipped} skipped",
        detail=(
            "New hosts: type Auto, no scrape address, source=zabbix. Owner, contact, email, phone, "
            "notes and filled extras on existing assets are never changed. Skipped = a second Zabbix "
            "host with the IP/name of an asset already linked to another Zabbix host."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {"synced": created + linked, "created": created, "linked": linked, "skipped": skipped}


def last_sync_entry(db: Session) -> JournalEntry | None:
    return db.query(JournalEntry).filter_by(module="zabbix", action="sync").order_by(JournalEntry.id.desc()).first()


def note_health(db: Session, status: dict[str, Any]) -> None:
    """One journal line when Zabbix goes down (or comes back). Repeats are skipped."""
    if not status.get("configured"):
        return
    healthy = bool(status.get("ok"))
    last = (
        db.query(JournalEntry).filter_by(module="zabbix", action="health").order_by(JournalEntry.id.desc()).first()
    )
    if healthy:
        if last is None or last.status == "ok":
            return
        report(db, "zabbix", "health", "ok", summary="Zabbix API reachable again", detail=str(status.get("why") or ""))
        return
    if last is not None and last.status == "warn":
        return
    report(
        db,
        "zabbix",
        "health",
        "warn",
        summary=f"Zabbix {str(status.get('label') or 'unreachable').lower()} — Core keeps working",
        detail=(
            f"{status.get('why') or ''} Incidents, mail and the UI are unaffected; "
            "Zabbix calls back off for 2 minutes instead of retrying."
        ).strip(),
    )


def current_status(*, force: bool = False) -> dict[str, Any]:
    return zabbix_status(settings.zabbix_url, settings.zabbix_token, timeout=settings.zabbix_timeout, force=force)


def _open_zabbix_incidents(db: Session) -> list[Incident]:
    from app.services import ACTIVE_INCIDENT_STATUSES

    return (
        db.query(Incident)
        .filter(Incident.source == "zabbix", Incident.status.in_(ACTIVE_INCIDENT_STATUSES))
        .order_by(Incident.id)
        .limit(500)
        .all()
    )


def _triggerid(incident: Incident) -> str:
    labels = (incident.alert_payload or {}).get("labels") if isinstance(incident.alert_payload, dict) else {}
    found = str((labels or {}).get("zabbix_triggerid") or "").strip()
    if found:
        return found
    parts = str(incident.fingerprint or "").split(":", 2)
    if len(parts) == 3 and parts[0] == "zabbix" and parts[1].isdigit():
        return parts[1]
    return ""


def poll_problems(db: Session) -> int:
    """Backup for a missed recovery webhook: problem.get for open Zabbix incidents only.

    A trigger with no unresolved problem left → the same recovery path as the
    webhook (same fingerprint), so it is RESOLVED once and never duplicated.
    """
    from app.services import ingest_alertmanager

    incidents = _open_zabbix_incidents(db)
    by_trigger: dict[str, list[Incident]] = {}
    cutoff = utcnow() - RESOLVE_GRACE
    for incident in incidents:
        started = incident.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=cutoff.tzinfo)
        if started is not None and started > cutoff:
            continue
        trigger = _triggerid(incident)
        if trigger:
            by_trigger.setdefault(trigger, []).append(incident)
    if not by_trigger:
        return 0
    active = active_problem_triggerids(
        settings.zabbix_url, settings.zabbix_token, list(by_trigger), timeout=settings.zabbix_timeout
    )
    alerts = []
    for trigger, rows in by_trigger.items():
        if trigger in active:
            continue
        for incident in rows:
            payload = incident.alert_payload if isinstance(incident.alert_payload, dict) else {}
            labels = dict(payload.get("labels") or {})
            alerts.append(
                {
                    "status": "resolved",
                    "labels": labels,
                    "annotations": dict(payload.get("annotations") or {}),
                    "forge": {"fingerprint": incident.fingerprint, "source": "zabbix"},
                }
            )
    if not alerts:
        return 0
    ingest_alertmanager(db, {"status": "resolved", "alerts": alerts}, source="zabbix")
    report(
        db,
        "zabbix",
        "poll",
        "ok",
        summary=f"Zabbix backup poll resolved {len(alerts)} incident(s) with no open problem",
        detail="problem.get found no unresolved problem for these triggers (recovery webhook missed).",
    )
    return len(alerts)


def refresh_agents(db: Session) -> int:
    """Light host.get (interfaces only) for linked assets → Zabbix agent pill. Not per row."""
    linked = [row for row in db.query(Asset).filter(Asset.zabbix_hostid != "").all() if (row.zabbix_hostid or "").strip()]
    if not linked:
        return 0

    version = server_version(settings.zabbix_url, settings.zabbix_timeout)
    output = ["hostid", "active_available"] if version >= (6, 4) else ["hostid"]
    rows = call(
        "host.get",
        {
            "output": output,
            "hostids": sorted({row.zabbix_hostid for row in linked}),
            "selectInterfaces": ["type", "available"],
        },
        url=settings.zabbix_url,
        token=settings.zabbix_token,
        timeout=settings.zabbix_timeout,
    ) or []
    states = {str(row.get("hostid") or ""): agent_state(row) for row in rows if isinstance(row, dict)}
    changed = 0
    for asset in linked:
        state = states.get(asset.zabbix_hostid, "unknown")
        if asset.zabbix_agent != state:
            asset.zabbix_agent = state
            changed += 1
    if changed:
        db.commit()
    return changed


def maybe_poll(db: Session, *, now: float | None = None) -> dict[str, Any]:
    """Called from the jobs loop every ~2 s; does real work at most every 3 / 5 minutes.

    Problem poll only when the webhook token is set (webhook in use). During a
    Zabbix outage the client backoff makes these fail fast without network.
    """
    out: dict[str, Any] = {"problems": None, "agents": None}
    if not settings.zabbix_enabled:
        return out
    mono = time.monotonic() if now is None else now
    if settings.zabbix_webhook_token and mono - _poll_state["problems"] >= PROBLEM_POLL_SECONDS:
        _poll_state["problems"] = mono
        try:
            out["problems"] = poll_problems(db)
        except Exception as exc:
            log.debug("zabbix problem poll skipped: %s", exc)
            db.rollback()
            out["problems"] = str(exc)
    if mono - _poll_state["agents"] >= AGENT_POLL_SECONDS:
        _poll_state["agents"] = mono
        try:
            out["agents"] = refresh_agents(db)
        except Exception as exc:
            log.debug("zabbix agent refresh skipped: %s", exc)
            db.rollback()
            out["agents"] = str(exc)
    return out


def reset_poll_state() -> None:
    _poll_state["problems"] = 0.0
    _poll_state["agents"] = 0.0
