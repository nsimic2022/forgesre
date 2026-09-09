from __future__ import annotations

import logging
import re
import time

from sqlalchemy.orm import Session

from app.audit import audit
from app.demo_ids import DEMO_CANDIDATE_IP, is_lab_inventory, is_lab_inventory_row
from app.exporter_detect import AUTO_ASSET_TYPE, detect_exporter, is_auto_asset_type
from app.journal import report
from app.models import Asset, DiscoveryCandidate, Incident, ScheduledReport, utcnow
from app.settings import settings

log = logging.getLogger("forgesre")
_discovery_loop_mono: float = 0.0
LINUX_EXPORTER_PORT = 9100
WINDOWS_EXPORTER_PORT = 9182
DEMO_CANDIDATE_NOTES = (
    "DEMO discovery seed (10.20.30.41). Not a real machine. "
    "Lab only — not scraped. Used so /discovery has an Approve click."
)
CANDIDATE_ROLE_CHOICES = [
    "Possible Linux server",
    "Possible Windows server",
    "Possible network device",
    "Possible web/appliance",
    "Unknown device",
]
CANDIDATE_ROLE_CHOICES = [
    "Possible Linux server",
    "Possible Windows server",
    "Possible network device",
    "Possible web/appliance",
    "Unknown device",
]


def asset_kind(type: str = "", profile: str = "") -> str:
    """linux | windows | network | web | unknown | other. Windows is checked before 'server' (Windows Server)."""
    blob = f"{type} {profile}".lower()
    if is_auto_asset_type(type):
        return "unknown"
    if "unknown" in blob:
        return "unknown"
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


_ASSET_TYPE_ABBREV = {
    "linux": "lnx",
    "windows": "win",
    "network": "net",
    "web": "web",
}


def asset_type_abbrev(type: str = "") -> str:
    """Short Type label for the Assets table. Add/Edit dropdowns keep full names."""
    label = (type or "").strip()
    if not label:
        return "—"
    return _ASSET_TYPE_ABBREV.get(asset_kind(label), label)


def default_scrape_address(type: str, ip: str, profile: str = "") -> str:
    ip = (ip or "").strip()
    if not ip:
        return ""
    kind = asset_kind(type, profile)
    if kind == "windows":
        return f"{ip}:{WINDOWS_EXPORTER_PORT}"
    if kind == "linux":
        return f"{ip}:{LINUX_EXPORTER_PORT}"
    return ""


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
    if kind == "unknown":
        return ""
    return "network-switch"


def sd_targets(db: Session, core_address: str | None = None) -> list[dict]:
    """Inventory scrape targets only. Core stays on Prometheus static_configs so a Core outage does not lose the demo scrape.

    Linux node_exporter (:9100, job=linux-standard) and Windows windows_exporter
    (:9182, job=windows-standard) share this HTTP SD. Seeded forge-demo-* hosts
    and the discovery Approve seed 10.20.30.41 are lab-only and never appear
    here, even if scrape_address is set.
    """
    del core_address
    targets: list[dict] = []
    seen: set[str] = set()
    for asset in db.query(Asset).order_by(Asset.asset_id).all():
        if is_lab_inventory_row(asset):
            continue
        address = (asset.scrape_address or "").strip()
        if not address or address in seen:
            continue
        seen.add(address)
        profile = asset.monitoring_profile or default_monitoring_profile(asset.type or "")
        targets.append(
            {
                "targets": [address],
                "labels": {
                    "asset": asset.asset_id,
                    "job": profile,
                    "monitoring_profile": profile,
                    "source": asset.source or "manual",
                },
            }
        )
    return targets


def is_snmp_asset(asset: Asset) -> bool:
    """Network devices with an IP are polled by snmp_exporter. Linux/Windows HTTP exporters are not."""
    if not settings.snmp_enabled:
        return False
    if is_lab_inventory_row(asset):
        return False
    ip = (asset.ip or "").strip()
    if not ip:
        return False
    kind = asset_kind(asset.type or "", asset.monitoring_profile or "")
    if kind in {"linux", "windows", "web"}:
        return False
    return kind == "network"


def sd_snmp_targets(db: Session) -> list[dict]:
    """Prometheus HTTP SD for snmp_exporter. Target address is the device IP; Prometheus relabels to the exporter."""
    if not settings.snmp_enabled:
        return []
    targets: list[dict] = []
    seen: set[str] = set()
    for asset in db.query(Asset).order_by(Asset.asset_id).all():
        if not is_snmp_asset(asset):
            continue
        ip = asset.ip.strip()
        if ip in seen:
            continue
        seen.add(ip)
        targets.append(
            {
                "targets": [ip],
                "labels": {
                    "asset": asset.asset_id,
                    "monitoring_profile": asset.monitoring_profile or "network-switch",
                    "source": asset.source or "manual",
                    "snmp_module": settings.snmp_module,
                    "snmp_auth": "public_v2",
                },
            }
        )
    return targets


def upsert_candidate(
    db: Session,
    ip: str,
    role: str,
    ports: list[int],
    source: str = "scan",
    *,
    snmp_ok: bool = False,
    node_exporter: bool = False,
    windows_exporter: bool = False,
) -> DiscoveryCandidate:
    row = db.query(DiscoveryCandidate).filter_by(ip=ip).first()
    if row is None:
        row = DiscoveryCandidate(
            ip=ip,
            proposed_role=role,
            open_ports=list(ports or []),
            snmp_ok=bool(snmp_ok),
            node_exporter=bool(node_exporter),
            windows_exporter=bool(windows_exporter),
            status="new",
            source=source,
        )
        db.add(row)
    elif row.status == "new":
        row.proposed_role = role
        row.open_ports = list(ports or [])
        row.snmp_ok = bool(snmp_ok)
        row.node_exporter = bool(node_exporter)
        row.windows_exporter = bool(windows_exporter)
        row.seen_at = utcnow()
    return row


def seed_demo_candidate(db: Session) -> DiscoveryCandidate:
    existing_asset = db.query(Asset).filter_by(ip=DEMO_CANDIDATE_IP).first()
    row = upsert_candidate(
        db,
        DEMO_CANDIDATE_IP,
        "Possible Linux server",
        [22, 9100],
        source="demo",
        node_exporter=True,
    )
    if existing_asset:
        row.status = "approved"
        row.asset_id = existing_asset.asset_id
        if not (existing_asset.notes or "").strip():
            existing_asset.notes = DEMO_CANDIDATE_NOTES
    db.commit()
    db.refresh(row)
    return row


def mark_discovery_loop_alive() -> None:
    """Heartbeat so doctor can tell the discovery thread is still running."""
    global _discovery_loop_mono
    _discovery_loop_mono = time.monotonic()


def discovery_loop_age_seconds() -> float | None:
    if _discovery_loop_mono <= 0:
        return None
    return time.monotonic() - _discovery_loop_mono


def run_scan(
    db: Session,
    cidrs: list[str] | None = None,
    *,
    merge_auto: bool = False,
    ip_output: str | None = None,
) -> dict:
    """Probe hosts from confirmed YAML ``discovery.cidrs`` only.

    Empty cidrs = no scan (does not silently scan all VLANs). ``merge_auto`` is
    ignored (kept for call-site compatibility with the job worker).
    """
    from discovery import normalize_cidrs, probe_host, scan_plan

    _ = (merge_auto, ip_output)
    yaml_side = list(cidrs) if cidrs is not None else list(settings.discovery_cidrs)
    cleaned = normalize_cidrs(yaml_side)
    plan = scan_plan(cleaned)
    resolved = {
        "cidrs": cleaned,
        "yaml": cleaned,
        "auto": [],
        "source": "yaml" if cleaned else "none",
        "warnings": list(plan["warnings"]),
        "hosts": list(plan["hosts"]),
        "host_count": int(plan["total"]),
        "truncated": bool(plan["truncated"]),
    }
    targets = list(resolved.get("cidrs") or [])
    hosts = list(resolved.get("hosts") or [])
    warnings = list(resolved.get("warnings") or [])
    if not targets:
        log.info("discovery scan skipped: empty discovery.cidrs")
        report(
            db,
            "discovery",
            "scan",
            "warn",
            summary="Scan skipped: no discovery.cidrs (Confirm a management /24 first)",
            detail="empty cidrs — not scanning all VLANs",
        )
        return {
            "found": 0,
            "skipped": 0,
            "cidrs": [],
            "yaml": [],
            "auto": [],
            "source": "none",
            "warnings": warnings,
            "skipped_reason": "empty_cidrs",
            "hosts_planned": 0,
            "truncated": False,
        }
    found = 0
    skipped = 0
    known_ips = {item.ip for item in db.query(Asset).all() if item.ip}
    for ip in hosts:
        if ip in known_ips:
            skipped += 1
            continue
        try:
            result = probe_host(ip)
        except Exception:
            log.exception("discovery probe_host failed ip=%s", ip)
            continue
        if not result["alive"]:
            continue
        kind = str(result.get("exporter_kind") or "")
        upsert_candidate(
            db,
            ip,
            result["proposed_role"],
            result["open_ports"],
            snmp_ok=bool(result.get("snmp_ok")),
            node_exporter=kind == "linux",
            windows_exporter=kind == "windows",
        )
        found += 1
    if settings.discovery_mode == "automatic":
        for row in db.query(DiscoveryCandidate).filter_by(status="new").all():
            approve_candidate(db, row, actor="system-automatic")
    db.commit()
    log.info("discovery scan cidrs=%s found=%s skipped=%s source=%s", targets, found, skipped, resolved.get("source"))
    detail = f"cidrs={targets} source={resolved.get('source')}"
    if warnings:
        detail = detail + f" warnings={warnings}"
    report(
        db,
        "discovery",
        "scan",
        "ok" if not warnings else "warn",
        summary=f"Scan finished found={found} skipped={skipped}",
        detail=detail,
    )
    return {
        "found": found,
        "skipped": skipped,
        "cidrs": targets,
        "yaml": list(resolved.get("yaml") or []),
        "auto": list(resolved.get("auto") or []),
        "source": str(resolved.get("source") or ""),
        "warnings": warnings,
        "hosts_planned": int(resolved.get("host_count") or len(hosts)),
        "truncated": bool(resolved.get("truncated")),
    }


def create_manual_asset(
    db: Session,
    hostname: str,
    ip: str = "",
    type: str = "Linux Server",
    environment: str = "Production",
    owner: str = "platform",
    contact_name: str = "",
    owner_email: str = "",
    owner_phone: str = "",
    notes: str = "",
    monitoring_profile: str = "",
    scrape_address: str = "",
    actor: str = "system",
    metrics_fetcher=None,
    snmp_ok: bool | None = None,
    snmp_prober=None,
    require_new: bool = False,
    cloned_from: str = "",
    alarms: dict | None = None,
    asset_id: str = "",
) -> Asset:
    hostname = (hostname or "").strip()
    ip = (ip or "").strip()
    if not hostname:
        raise ValueError("hostname is required")
    requested = (asset_id or "").strip()
    if requested:
        slug = validate_asset_id(requested)
    else:
        slug = validate_asset_id(asset_id_slug(hostname, ip))
    existing = db.query(Asset).filter((Asset.asset_id == slug) | ((Asset.ip == ip) & (Asset.ip != ""))).first()
    if existing is None and require_new:
        existing = db.query(Asset).filter(Asset.hostname == hostname).first()
    if existing:
        if require_new:
            raise ValueError(
                f"{existing.asset_id} already uses this asset id, hostname, or IP — "
                "change Asset ID, hostname, or IP before Save"
            )
        report(
            db,
            "inventory",
            "asset.create",
            "warn",
            summary=f"{existing.asset_id} already exists — not duplicated",
            object_type="asset",
            object_id=existing.asset_id,
        )
        return existing
    detect_message = ""
    if is_auto_asset_type(type):
        detected = detect_exporter(
            ip,
            hint_type=type,
            fetcher=metrics_fetcher,
            snmp_ok=snmp_ok,
            snmp_prober=snmp_prober,
        )
        detect_message = detected.message
        if detected.kind == "windows":
            type = detected.asset_type
            monitoring_profile = monitoring_profile or detected.profile
            scrape_address = (scrape_address or "").strip() or detected.scrape_address
        elif detected.kind == "linux":
            type = detected.asset_type
            monitoring_profile = monitoring_profile or detected.profile
            scrape_address = (scrape_address or "").strip() or detected.scrape_address
        elif detected.kind == "network":
            type = detected.asset_type
            monitoring_profile = monitoring_profile or detected.profile
            scrape_address = (scrape_address or "").strip()
        else:
            type = "Unknown"
            monitoring_profile = (monitoring_profile or "").strip()
            scrape_address = (scrape_address or "").strip()
    profile = default_monitoring_profile(type, monitoring_profile)
    address = (scrape_address or "").strip() or default_scrape_address(type, ip, profile)
    stored_alarms: dict = {}
    if alarms is not None:
        from app.asset_alarms import normalize_alarms

        stored_alarms = normalize_alarms(alarms, asset_kind(type, profile))
    asset = Asset(
        asset_id=slug,
        hostname=hostname,
        ip=ip,
        type=type or "Linux Server",
        environment=environment or "Production",
        status="healthy",
        monitoring_profile=profile,
        owner=owner or "platform",
        contact_name=(contact_name or "").strip(),
        owner_email=(owner_email or "").strip(),
        owner_phone=(owner_phone or "").strip(),
        notes=(notes or "").strip(),
        source="manual",
        scrape_address=address,
        alarms=stored_alarms,
    )
    db.add(asset)
    audit(
        db,
        "asset.create",
        actor=actor,
        object_type="asset",
        object_id=asset.asset_id,
        data={"ip": ip, "cloned_from": cloned_from} if cloned_from else {"ip": ip},
    )
    db.commit()
    db.refresh(asset)
    if detect_message:
        setattr(asset, "_detect_message", detect_message)
    contact = asset.owner_email or asset.owner or "no owner email"
    report(
        db,
        "inventory",
        "asset.create",
        "ok",
        summary=f"Saved {asset.hostname} ({contact})",
        detail=(
            f"ip={asset.ip} scrape={asset.scrape_address} actor={actor} "
            f"detect={detect_message or '—'} cloned_from={cloned_from or '—'}"
        ),
        object_type="asset",
        object_id=asset.asset_id,
    )
    if is_snmp_asset(asset):
        report(
            db,
            "snmp",
            "target.add",
            "ok",
            summary=f"{asset.hostname} queued for snmp_exporter UDP/161",
            detail=f"ip={asset.ip} module={settings.snmp_module}",
            object_type="asset",
            object_id=asset.asset_id,
        )
    return asset


def update_asset(
    db: Session,
    asset: Asset,
    *,
    ip: str | None = None,
    type: str | None = None,
    environment: str | None = None,
    owner: str | None = None,
    contact_name: str | None = None,
    owner_email: str | None = None,
    owner_phone: str | None = None,
    notes: str | None = None,
    scrape_address: str | None = None,
    hostname: str | None = None,
    actor: str = "system",
    detect: bool = False,
    metrics_fetcher=None,
    snmp_ok: bool | None = None,
    snmp_prober=None,
    alarms: dict | None = None,
) -> Asset:
    old_ip = asset.ip or ""
    old_type = asset.type or ""
    old_profile = asset.monitoring_profile or ""
    old_scrape = asset.scrape_address or ""
    if hostname is not None:
        hostname = hostname.strip()
        if hostname:
            asset.hostname = hostname
    if ip is not None:
        asset.ip = ip.strip()
    if type is not None and type.strip():
        asset.type = type.strip()
    if environment is not None and environment.strip():
        asset.environment = environment.strip()
    if owner is not None:
        asset.owner = owner.strip() or "platform"
    if contact_name is not None:
        asset.contact_name = contact_name.strip()
    if owner_email is not None:
        asset.owner_email = owner_email.strip()
    if owner_phone is not None:
        asset.owner_phone = owner_phone.strip()
    if notes is not None:
        asset.notes = notes.strip()
    detect_message = ""
    auto = is_auto_asset_type(asset.type or "")
    if detect or auto:
        detected = detect_exporter(
            asset.ip,
            hint_type=old_type if not auto else "",
            hint_profile=old_profile,
            fetcher=metrics_fetcher,
            snmp_ok=snmp_ok,
            snmp_prober=snmp_prober,
        )
        detect_message = detected.message
        setattr(asset, "_detect_message", detect_message)
        if detected.kind:
            asset.type = detected.asset_type
            if detected.profile and (auto or (asset.monitoring_profile or "") in {"", "linux-standard", "windows-standard"}):
                asset.monitoring_profile = detected.profile
            if scrape_address is None or not (scrape_address or "").strip():
                asset.scrape_address = detected.scrape_address
                scrape_address = detected.scrape_address
        elif auto:
            asset.type = "Unknown"
            if scrape_address is None:
                asset.scrape_address = ""
                scrape_address = ""
    if scrape_address is not None:
        asset.scrape_address = scrape_address.strip()
    elif old_ip and asset.ip:
        if asset.scrape_address == f"{old_ip}:{LINUX_EXPORTER_PORT}":
            asset.scrape_address = f"{asset.ip}:{LINUX_EXPORTER_PORT}"
        elif asset.scrape_address == f"{old_ip}:{WINDOWS_EXPORTER_PORT}":
            asset.scrape_address = f"{asset.ip}:{WINDOWS_EXPORTER_PORT}"
    new_kind = asset_kind(asset.type or "", asset.monitoring_profile or "")
    old_kind = asset_kind(old_type, old_profile)
    if scrape_address is None and not detect and not auto and asset.ip:
        old_default = default_scrape_address(old_type, asset.ip, old_profile) or default_scrape_address(
            old_type, old_ip, old_profile
        )
        if old_kind != new_kind and (old_scrape == old_default or old_scrape == default_scrape_address(old_type, old_ip, old_profile)):
            remapped = default_scrape_address(asset.type or "", asset.ip, asset.monitoring_profile or "")
            if remapped or new_kind == "network":
                asset.scrape_address = remapped
    if new_kind == "windows" and (asset.monitoring_profile or "") in {"", "linux-standard"}:
        asset.monitoring_profile = "windows-standard"
    elif new_kind == "linux" and (asset.monitoring_profile or "") in {"", "windows-standard"}:
        asset.monitoring_profile = "linux-standard"
    elif new_kind == "network" and (asset.monitoring_profile or "") in {"", "linux-standard", "windows-standard"}:
        asset.monitoring_profile = "network-switch"
    elif new_kind == "unknown":
        if (asset.monitoring_profile or "") in {"linux-standard", "windows-standard", "network-switch"}:
            asset.monitoring_profile = ""
    if alarms is not None:
        from app.asset_alarms import normalize_alarms

        asset.alarms = normalize_alarms(alarms, new_kind)
    audit(
        db,
        "asset.update",
        actor=actor,
        object_type="asset",
        object_id=asset.asset_id,
        data={"owner": asset.owner, "owner_email": asset.owner_email},
    )
    db.commit()
    db.refresh(asset)
    if detect_message:
        setattr(asset, "_detect_message", detect_message)
    report(
        db,
        "inventory",
        "asset.update",
        "ok",
        summary=f"Updated {asset.hostname} owner={asset.owner} email={asset.owner_email or '—'}",
        detail=f"actor={actor} phone={asset.owner_phone} detect={detect_message or '—'}",
        object_type="asset",
        object_id=asset.asset_id,
    )
    return asset


def persist_live_classification(db: Session, asset: Asset, probe: object) -> bool:
    """Save type + scrape_address when verify/detect saw node_ or windows_ live.

    Prometheus HTTP SD only lists rows with a scrape_address. An IP-only Unknown
    row is reachable by ICMP but never scraped until this write.
    """
    from app.asset_probe import AssetProbe, asset_as_probe_item, classification_patch

    if is_lab_inventory_row(asset):
        return False
    if not isinstance(probe, AssetProbe):
        return False
    patch = classification_patch(asset_as_probe_item(asset), probe)
    if not patch:
        return False
    if "type" in patch:
        asset.type = patch["type"]
    if "monitoring_profile" in patch:
        asset.monitoring_profile = patch["monitoring_profile"]
    if "scrape_address" in patch:
        asset.scrape_address = patch["scrape_address"]
    audit(
        db,
        "asset.update",
        actor="verify",
        object_type="asset",
        object_id=asset.asset_id,
        data=patch,
    )
    report(
        db,
        "inventory",
        "asset.update",
        "ok",
        summary=f"Verify classified {asset.hostname} as {asset.type}",
        detail=f"scrape={asset.scrape_address} patch={patch}",
        object_type="asset",
        object_id=asset.asset_id,
    )
    return True


def similar_incident_groups(db: Session, asset: Asset) -> list[dict]:
    rows = (
        db.query(Incident)
        .filter(Incident.asset_id == asset.id)
        .order_by(Incident.started_at.desc(), Incident.id.desc())
        .all()
    )
    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in rows:
        payload = item.alert_payload if isinstance(item.alert_payload, dict) else {}
        labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
        alertname = str(labels.get("alertname") or "").strip()
        title = (item.title or "").strip() or alertname or "Incident"
        key = (alertname or title).strip().lower()
        if key not in groups:
            order.append(key)
            groups[key] = {
                "key": key,
                "title": title,
                "alertname": alertname,
                "count": 0,
                "open_count": 0,
                "last_number": item.number,
                "last_status": item.status,
                "last_severity": item.severity,
                "last_started_at": item.started_at.isoformat() if item.started_at else None,
                "incidents": [],
            }
        group = groups[key]
        group["count"] += 1
        if item.status not in {"RESOLVED", "CLOSED"}:
            group["open_count"] += 1
        group["incidents"].append(
            {
                "number": item.number,
                "title": item.title,
                "status": item.status,
                "severity": item.severity,
                "started_at": item.started_at.isoformat() if item.started_at else None,
            }
        )
    return [groups[key] for key in order]


def approve_candidate(db: Session, row: DiscoveryCandidate, actor: str) -> Asset:
    slug = _asset_id_from_ip(row.ip)
    asset = db.query(Asset).filter((Asset.asset_id == slug) | (Asset.ip == row.ip)).first()
    if asset is None:
        role = row.proposed_role or ""
        ports = {int(p) for p in (row.open_ports or []) if str(p).isdigit() or isinstance(p, int)}
        has_windows = bool(getattr(row, "windows_exporter", False))
        has_node = bool(getattr(row, "node_exporter", False))
        if has_windows or "Windows" in role:
            confirmed = has_windows or ("no windows_exporter" not in role and "pick OS" not in role)
            atype, profile, scrape = (
                "Windows Server",
                "windows-standard",
                (f"{row.ip}:{WINDOWS_EXPORTER_PORT}" if confirmed else ""),
            )
        elif has_node or "Linux" in role:
            confirmed = has_node or ("no node_exporter" not in role and "pick OS" not in role)
            atype, profile, scrape = (
                "Linux Server",
                "linux-standard",
                (
                    f"{row.ip}:{LINUX_EXPORTER_PORT}"
                    if confirmed and (has_node or LINUX_EXPORTER_PORT in ports)
                    else ""
                ),
            )
        elif bool(getattr(row, "snmp_ok", False)) or "network" in role.lower():
            atype, profile, scrape = "Network device", "network-switch", ""
        elif "pick OS" in role:
            atype, profile, scrape = "Unknown", "", ""
        else:
            atype, profile, scrape = "Web/appliance", "web-standard", ""
        hostname = (getattr(row, "hostname", None) or "").strip() or slug
        notes = (
            DEMO_CANDIDATE_NOTES
            if is_lab_inventory(asset_id=slug, hostname=hostname, ip=row.ip, scrape_address=scrape)
            else ""
        )
        asset = Asset(
            asset_id=slug,
            hostname=hostname,
            ip=row.ip,
            type=atype,
            environment="Production",
            status="healthy",
            monitoring_profile=profile,
            owner="platform",
            source="discovery",
            scrape_address=scrape,
            notes=notes,
        )
        db.add(asset)
        db.flush()
    row.status = "approved"
    row.decided_by = actor
    row.decided_at = utcnow()
    row.asset_id = asset.asset_id
    audit(
        db,
        "discovery.approve",
        actor=actor,
        object_type="asset",
        object_id=asset.asset_id,
        data={"ip": row.ip},
    )
    db.commit()
    db.refresh(asset)
    report(
        db,
        "discovery",
        "approve",
        "ok",
        summary=f"Approved {row.ip} → {asset.asset_id}",
        detail=f"actor={actor} scrape={asset.scrape_address}",
        object_type="asset",
        object_id=asset.asset_id,
    )
    if is_snmp_asset(asset):
        report(
            db,
            "snmp",
            "target.add",
            "ok",
            summary=f"{asset.asset_id} queued for snmp_exporter UDP/161",
            detail=f"ip={asset.ip} module={settings.snmp_module}",
            object_type="asset",
            object_id=asset.asset_id,
        )
    return asset


def ignore_candidate(db: Session, row: DiscoveryCandidate, actor: str) -> None:
    row.status = "ignored"
    row.decided_by = actor
    row.decided_at = utcnow()
    audit(db, "discovery.ignore", actor=actor, object_type="candidate", object_id=row.ip)
    db.commit()
    report(
        db,
        "discovery",
        "ignore",
        "ok",
        summary=f"Ignored candidate {row.ip}",
        object_type="candidate",
        object_id=row.ip,
    )


def suggest_clone_candidate_ip(db: Session, ip: str) -> str:
    """Next unused IPv4-looking address for Clone. Does not ping the network."""
    used = {item.ip for item in db.query(DiscoveryCandidate).all() if item.ip}
    parts = (ip or "").strip().split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        last = int(parts[3])
        prefix = ".".join(parts[:3])
        for step in range(1, 256):
            candidate = f"{prefix}.{(last + step) % 256}"
            if candidate not in used:
                return candidate
    n = 2
    base = (ip or "10.0.0.1").strip() or "10.0.0.1"
    while True:
        candidate = f"{base}-{n}"
        if candidate not in used:
            return candidate
        n += 1


def update_candidate(
    db: Session,
    row: DiscoveryCandidate,
    *,
    ip: str,
    hostname: str = "",
    proposed_role: str = "",
    actor: str = "",
) -> DiscoveryCandidate:
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("IP is required")
    clash = (
        db.query(DiscoveryCandidate)
        .filter(DiscoveryCandidate.ip == ip, DiscoveryCandidate.id != row.id)
        .first()
    )
    if clash is not None:
        raise ValueError("IP already exists")
    row.ip = ip
    row.hostname = (hostname or "").strip()
    role = (proposed_role or "").strip()
    if role:
        row.proposed_role = role
    audit(db, "discovery.update", actor=actor, object_type="candidate", object_id=row.ip)
    db.commit()
    db.refresh(row)
    report(
        db,
        "discovery",
        "update",
        "ok",
        summary=f"Updated candidate {row.ip}",
        object_type="candidate",
        object_id=row.ip,
    )
    return row


def clone_candidate(
    db: Session,
    row: DiscoveryCandidate,
    *,
    ip: str,
    hostname: str = "",
    proposed_role: str = "",
    actor: str = "",
) -> DiscoveryCandidate:
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("IP is required")
    if db.query(DiscoveryCandidate).filter_by(ip=ip).first() is not None:
        raise ValueError("IP already exists")
    copy = DiscoveryCandidate(
        ip=ip,
        hostname=(hostname or "").strip(),
        proposed_role=(proposed_role or "").strip() or row.proposed_role,
        open_ports=list(row.open_ports or []),
        snmp_ok=bool(getattr(row, "snmp_ok", False)),
        node_exporter=bool(getattr(row, "node_exporter", False)),
        windows_exporter=bool(getattr(row, "windows_exporter", False)),
        status="new",
        source=row.source if row.source == "demo" else "scan",
    )
    db.add(copy)
    db.flush()
    audit(db, "discovery.clone", actor=actor, object_type="candidate", object_id=copy.ip)
    db.commit()
    db.refresh(copy)
    report(
        db,
        "discovery",
        "clone",
        "ok",
        summary=f"Cloned candidate {row.ip} → {copy.ip}",
        object_type="candidate",
        object_id=copy.ip,
    )
    return copy


def delete_candidate(db: Session, row: DiscoveryCandidate, actor: str) -> str:
    """Delete the candidate row. Does not remove an approved asset."""
    ip = row.ip
    db.delete(row)
    audit(db, "discovery.remove", actor=actor, object_type="candidate", object_id=ip)
    db.commit()
    report(
        db,
        "discovery",
        "remove",
        "ok",
        summary=f"Removed candidate {ip}",
        object_type="candidate",
        object_id=ip,
    )
    return ip


def sync_netbox(db: Session) -> dict:
    if not settings.netbox_enabled:
        return {"synced": 0, "skipped": True}
    from app.netbox import list_devices

    try:
        devices = list_devices(settings.netbox_url, settings.netbox_token)
    except Exception as exc:
        from app.netbox import format_client_error

        detail = format_client_error(exc, settings.netbox_token)
        summary = "NetBox sync failed: HTTP 403" if "403" in detail else "NetBox sync failed"
        log.warning("netbox sync failed: %s", detail)
        report(db, "netbox", "sync", "error", summary=summary, detail=detail)
        return {"synced": 0, "error": detail}
    count = 0
    for device in devices:
        slug = device["name"]
        asset = db.query(Asset).filter((Asset.netbox_id == device["netbox_id"]) | (Asset.asset_id == slug)).first()
        if asset is None:
            asset = Asset(
                asset_id=slug,
                hostname=slug,
                ip=device.get("ip") or "",
                type=device.get("type") or "device",
                environment="Production",
                status="healthy" if device.get("status") == "active" else "offline",
                monitoring_profile="linux-standard",
                owner="netbox",
                source="netbox",
                netbox_id=device["netbox_id"],
                scrape_address=f"{device['ip']}:9100" if device.get("ip") else "",
            )
            db.add(asset)
            count += 1
        else:
            asset.netbox_id = device["netbox_id"]
            asset.source = asset.source or "netbox"
            if device.get("ip"):
                asset.ip = device["ip"]
            count += 1
    db.commit()
    report(
        db,
        "netbox",
        "sync",
        "ok",
        summary=f"NetBox sync wrote {count} asset(s)",
    )
    return {"synced": count}


def _asset_id_from_ip(ip: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", ip)
    return f"disc-{safe}"


ASSET_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_asset_id(value: str) -> str:
    return (value or "").strip().lower()


def validate_asset_id(value: str) -> str:
    """Short unique inventory id: lowercase letters, digits, hyphens (e.g. win10-gp)."""
    slug = normalize_asset_id(value)
    if not slug:
        raise ValueError("asset_id is required")
    if not ASSET_ID_RE.fullmatch(slug):
        raise ValueError(
            "asset_id must be lowercase letters, digits, and hyphens "
            "(e.g. win10-gp), 1–63 characters, not starting or ending with a hyphen"
        )
    return slug


def asset_id_slug(hostname: str, ip: str = "") -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", hostname or "").strip("-").lower()
    return slug or _asset_id_from_ip(ip or "asset")


ASSET_TYPE_CHOICES = [
    AUTO_ASSET_TYPE,
    "Linux Server",
    "Windows Server",
    "Network device",
    "Web/appliance",
]


def suggest_clone_hostname(db: Session, asset: Asset) -> str:
    """New hostname for Clone. Lab forge-demo-* ids are stripped so the copy is a real asset."""
    from app.seed import DEMO_ASSET_PREFIX, is_demo_asset_id

    raw = (asset.hostname or asset.asset_id or "asset").strip()
    if is_demo_asset_id(raw) or is_demo_asset_id(asset.asset_id):
        rest = raw
        if rest.lower().startswith(DEMO_ASSET_PREFIX):
            rest = rest[len(DEMO_ASSET_PREFIX) :]
        base = f"copy-{rest}".strip("-") or "copy-host"
    else:
        base = f"{raw}-copy"
    candidate = base
    n = 2
    while True:
        clash = db.query(Asset).filter(Asset.hostname == candidate).first()
        if clash is None:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def suggest_clone_asset_id(db: Session, asset: Asset) -> str:
    """New short asset_id for Clone. Independent of hostname."""
    from app.seed import DEMO_ASSET_PREFIX, is_demo_asset_id

    raw = (asset.asset_id or "asset").strip()
    if is_demo_asset_id(raw):
        rest = raw
        if rest.lower().startswith(DEMO_ASSET_PREFIX):
            rest = rest[len(DEMO_ASSET_PREFIX) :]
        base = f"copy-{rest}".strip("-") or "copy-asset"
    else:
        base = f"{raw}-copy"
    try:
        candidate = validate_asset_id(base)
    except ValueError:
        candidate = "copy-asset"
    n = 2
    while db.query(Asset).filter_by(asset_id=candidate).first() is not None:
        try:
            candidate = validate_asset_id(f"{base}-{n}")
        except ValueError:
            candidate = f"copy-asset-{n}"
        n += 1
    return candidate


def clone_prefill(db: Session, asset: Asset) -> dict:
    """Form values for Clone. User can tweak before Save. Does not copy NetBox id or demo identity."""
    from app.seed import is_demo_asset_id

    notes = (asset.notes or "").strip()
    lab = is_demo_asset_id(asset.asset_id) or is_demo_asset_id(asset.hostname)
    if lab:
        lowered = notes.lower()
        if "not a real" in lowered or "seeded demo" in lowered or "lab incidents only" in lowered:
            notes = ""
    scrape = (asset.scrape_address or "").strip()
    if not scrape:
        scrape = default_scrape_address(asset.type or "", asset.ip or "", asset.monitoring_profile or "")
    return {
        "asset_id": suggest_clone_asset_id(db, asset),
        "hostname": suggest_clone_hostname(db, asset),
        "ip": asset.ip or "",
        "type": asset.type or AUTO_ASSET_TYPE,
        "environment": asset.environment or "Production",
        "owner": asset.owner or "platform",
        "contact_name": asset.contact_name or "",
        "owner_email": asset.owner_email or "",
        "owner_phone": asset.owner_phone or "",
        "notes": notes,
        "scrape_address": scrape,
        "cloned_from": asset.asset_id,
        "lab_source": lab,
        "alarms": _form_alarms(asset),
    }


def _form_alarms(asset: Asset | None = None) -> dict:
    from app.asset_alarms import default_alarms, normalize_alarms
    from app.asset_metrics import metric_class_for

    if asset is None:
        return default_alarms("linux")
    return normalize_alarms(getattr(asset, "alarms", None), metric_class_for(asset))


def asset_form_values(asset: Asset | None = None) -> dict:
    if asset is None:
        return {
            "asset_id": "",
            "hostname": "",
            "ip": "",
            "type": AUTO_ASSET_TYPE,
            "environment": "Production",
            "owner": "platform",
            "contact_name": "",
            "owner_email": "",
            "owner_phone": "",
            "notes": "",
            "scrape_address": "",
            "cloned_from": "",
            "lab_source": False,
            "alarms": _form_alarms(),
        }
    return {
        "asset_id": asset.asset_id or "",
        "hostname": asset.hostname or "",
        "ip": asset.ip or "",
        "type": asset.type or AUTO_ASSET_TYPE,
        "environment": asset.environment or "Production",
        "owner": asset.owner or "platform",
        "contact_name": asset.contact_name or "",
        "owner_email": asset.owner_email or "",
        "owner_phone": asset.owner_phone or "",
        "notes": asset.notes or "",
        "scrape_address": asset.scrape_address or "",
        "cloned_from": "",
        "lab_source": False,
        "alarms": _form_alarms(asset),
    }


def asset_search_blob(asset: Asset) -> str:
    return " ".join(
        [
            str(asset.number or ""),
            asset.asset_id or "",
            asset.hostname or "",
            asset.ip or "",
        ]
    ).lower()


def assets_matching(rows: list[Asset], q: str = "", status: str = "") -> list[Asset]:
    """Filter inventory by asset number, id, hostname, or IP (substring), and optional status."""
    needle = (q or "").strip().lower()
    wanted = (status or "").strip().lower()
    out = list(rows)
    if needle:
        out = [row for row in out if needle in asset_search_blob(row)]
    if wanted:
        out = [row for row in out if (row.status or "").lower() == wanted]
    return out


def delete_blocked(asset: Asset) -> str:
    """Lab forge-demo-* rows can be removed like any other asset."""
    del asset
    return ""


def delete_asset(db: Session, asset: Asset, actor: str = "system") -> dict:
    """Remove inventory + HTTP/SNMP SD targets. Incidents stay; the asset FK is cleared.

    Prometheus HTTP SD is live from this table — the next scrape drops the host.
    Core's static demo job is untouched. forge-demo-* rows can be removed; seed
    will not recreate an id that the operator already deleted.
    """
    reason = delete_blocked(asset)
    if reason:
        raise ValueError(reason)
    asset_id = asset.asset_id
    hostname = asset.hostname
    pk = asset.id
    number = asset.number
    scrape = asset.scrape_address or ""
    ip = asset.ip or ""
    snmp = is_snmp_asset(asset)
    unlinked = 0
    for item in db.query(Incident).filter_by(asset_id=pk).all():
        item.asset_id = None
        unlinked += 1
    for row in db.query(DiscoveryCandidate).filter(
        (DiscoveryCandidate.asset_id == asset_id) | ((DiscoveryCandidate.ip == ip) & (DiscoveryCandidate.ip != ""))
    ).all():
        row.asset_id = ""
        if row.status == "approved":
            row.status = "new"
            row.decided_by = ""
            row.decided_at = None
    for sched in db.query(ScheduledReport).all():
        ids = [str(x) for x in (sched.asset_ids or [])]
        if asset_id in ids:
            sched.asset_ids = [x for x in ids if x != asset_id]
    db.flush()
    db.delete(asset)
    audit(
        db,
        "asset.delete",
        actor=actor,
        object_type="asset",
        object_id=asset_id,
        data={"unlinked_incidents": unlinked, "scrape": scrape, "number": number},
    )
    db.commit()
    report(
        db,
        "inventory",
        "asset.delete",
        "ok",
        summary=f"Removed {hostname} ({asset_id})",
        detail=f"actor={actor} unlinked_incidents={unlinked} scrape={scrape or '—'}",
        object_type="asset",
        object_id=asset_id,
    )
    if snmp:
        report(
            db,
            "snmp",
            "target.remove",
            "ok",
            summary=f"{hostname} dropped from snmp_exporter",
            detail=f"ip={ip}",
            object_type="asset",
            object_id=asset_id,
        )
    return {"deleted": asset_id, "unlinked_incidents": unlinked}
