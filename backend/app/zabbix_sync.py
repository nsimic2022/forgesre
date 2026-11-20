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


def sync_zabbix(db: Session, *, force: bool = False) -> dict[str, Any]:
    """host.get → assets. Never writes to Zabbix.

    New host: type Auto, empty scrape, source=zabbix, zabbix_hostid set, no owner.
    Existing asset is matched by Zabbix host id, serial, MAC, name, then IP (app.asset_sources):
    empty IP / site / customer are filled and the agent pill updated; any other difference is a
    conflict to review. Owner, contact, email, phone and notes are never touched. With
    inventory.new_from_sources: review a new host waits in Discovery instead.
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

    from app.asset_sources import SourceRecord, build_index, hold_for_review, match, new_device_mode, open_conflict, reconcile

    index = build_index(db)
    review = new_device_mode() == "review"
    created = linked = skipped = held = conflicts = 0
    for host in hosts:
        hostid = host["hostid"]
        ip = str(host.get("ip") or "").strip()
        technical = str(host.get("host") or "").strip()
        rec = SourceRecord(source="zabbix", source_id=hostid, name=technical, sys_name=str(host.get("name") or ""),
                           ip="" if _loopback(ip) else ip, fields=group_extras(host.get("groups") or []))
        asset, disagreement = match(index, rec)
        if asset is None:
            asset = index.name.get(zabbix_asset_id(technical, hostid))
        if asset is not None and (asset.zabbix_hostid or "").strip() and asset.zabbix_hostid != hostid:
            if open_conflict(db, None, rec, "match", detail=f"looks like {asset.asset_id}, which is already Zabbix host {asset.zabbix_hostid}"):
                conflicts += 1
            skipped += 1
            continue
        if asset is None:
            if review and hold_for_review(db, rec, f"From Zabbix: {technical or hostid}"):
                held += 1
                continue
            asset = Asset(
                asset_id=_unique_asset_id(db, zabbix_asset_id(technical, hostid)),
                hostname=technical,
                ip=rec.ip,
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
                extras={},
            )
            db.add(asset)
            db.flush()
            created += 1
        else:
            if not (asset.source or "").strip():
                asset.source = "zabbix"
            asset.zabbix_agent = host.get("agent") or "unknown"
            linked += 1
        conflicts += reconcile(db, index, asset, rec, disagreement=disagreement)["conflicts"]
    db.commit()
    _poll_state["agents"] = time.monotonic()
    report(
        db,
        "zabbix",
        "sync",
        "ok",
        summary=f"Zabbix sync: {created} new, {linked} linked, {skipped} skipped"
                + (f", {held} waiting in Discovery" if held else "") + (f", {conflicts} to review" if conflicts else ""),
        detail=(
            "New hosts: type Auto, no scrape address, source=zabbix. Owner, contact, email, phone, "
            "notes and filled extras on existing assets are never changed. Skipped = a second Zabbix "
            "host with the IP/name of an asset already linked to another Zabbix host."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {"synced": created + linked, "created": created, "linked": linked, "skipped": skipped, "held": held, "conflicts": conflicts}


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
    from app.services import tracked_incident_statuses

    return (
        db.query(Incident)
        .filter(Incident.source == "zabbix", Incident.status.in_(tracked_incident_statuses()))
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
