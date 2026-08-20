from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.audit import audit
from app.models import Asset, DiscoveryCandidate, Incident
from app.settings import settings

log = logging.getLogger("forgesre")
DEMO_CANDIDATE_IP = "10.20.30.41"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sd_targets(db: Session, core_address: str | None = None) -> list[dict]:
    """Inventory scrape targets only. Core stays on Prometheus static_configs so a Core outage does not lose the demo scrape."""
    del core_address
    targets: list[dict] = []
    seen: set[str] = set()
    for asset in db.query(Asset).order_by(Asset.asset_id).all():
        address = (asset.scrape_address or "").strip()
        if not address or address in seen:
            continue
        seen.add(address)
        targets.append(
            {
                "targets": [address],
                "labels": {
                    "asset": asset.asset_id,
                    "job": asset.monitoring_profile or "linux-standard",
                    "monitoring_profile": asset.monitoring_profile or "linux-standard",
                    "source": asset.source or "manual",
                },
            }
        )
    return targets


def upsert_candidate(db: Session, ip: str, role: str, ports: list[int], source: str = "scan") -> DiscoveryCandidate:
    row = db.query(DiscoveryCandidate).filter_by(ip=ip).first()
    if row is None:
        row = DiscoveryCandidate(ip=ip, proposed_role=role, open_ports=ports, status="new", source=source)
        db.add(row)
    elif row.status == "new":
        row.proposed_role = role
        row.open_ports = ports
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
    )
    if existing_asset:
        row.status = "approved"
        row.asset_id = existing_asset.asset_id
    db.commit()
    db.refresh(row)
    return row


def run_scan(db: Session) -> dict:
    from discovery import hosts_from_cidrs, probe_host

    cidrs = settings.discovery_cidrs
    found = 0
    skipped = 0
    known_ips = {item.ip for item in db.query(Asset).all() if item.ip}
    for ip in hosts_from_cidrs(cidrs):
        if ip in known_ips:
            skipped += 1
            continue
        result = probe_host(ip)
        if not result["alive"]:
            continue
        upsert_candidate(db, ip, result["proposed_role"], result["open_ports"])
        found += 1
    if settings.discovery_mode == "automatic":
        for row in db.query(DiscoveryCandidate).filter_by(status="new").all():
            approve_candidate(db, row, actor="system-automatic")
    db.commit()
    log.info("discovery scan cidrs=%s found=%s skipped=%s", cidrs, found, skipped)
    return {"found": found, "skipped": skipped, "cidrs": cidrs}


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
) -> Asset:
    hostname = (hostname or "").strip()
    ip = (ip or "").strip()
    if not hostname:
        raise ValueError("hostname is required")
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", hostname).strip("-").lower() or _asset_id_from_ip(ip or "asset")
    existing = db.query(Asset).filter((Asset.asset_id == slug) | ((Asset.ip == ip) & (Asset.ip != ""))).first()
    if existing:
        return existing
    linux = "linux" in type.lower() or "server" in type.lower()
    profile = monitoring_profile or ("linux-standard" if linux else "network-switch")
    address = (scrape_address or "").strip() or (f"{ip}:9100" if linux and ip else "")
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
    )
    db.add(asset)
    audit(db, "asset.create", actor=actor, object_type="asset", object_id=asset.asset_id, data={"ip": ip})
    db.commit()
    db.refresh(asset)
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
    actor: str = "system",
) -> Asset:
    old_ip = asset.ip or ""
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
    if scrape_address is not None:
        asset.scrape_address = scrape_address.strip()
    elif old_ip and asset.ip and asset.scrape_address == f"{old_ip}:9100":
        asset.scrape_address = f"{asset.ip}:9100"
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
    return asset


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
        profile = "linux-standard" if "Linux" in (row.proposed_role or "") else "network-switch"
        asset = Asset(
            asset_id=slug,
            hostname=slug,
            ip=row.ip,
            type="Linux Server" if "Linux" in (row.proposed_role or "") else "Network device",
            environment="Production",
            status="healthy",
            monitoring_profile=profile,
            owner="platform",
            source="discovery",
            scrape_address=f"{row.ip}:9100" if "Linux" in (row.proposed_role or "") else "",
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
    return asset


def ignore_candidate(db: Session, row: DiscoveryCandidate, actor: str) -> None:
    row.status = "ignored"
    row.decided_by = actor
    row.decided_at = utcnow()
    audit(db, "discovery.ignore", actor=actor, object_type="candidate", object_id=row.ip)
    db.commit()


def sync_netbox(db: Session) -> dict:
    if not settings.netbox_enabled:
        return {"synced": 0, "skipped": True}
    from app.netbox import list_devices

    try:
        devices = list_devices(settings.netbox_url, settings.netbox_token)
    except Exception as exc:
        log.warning("netbox sync failed: %s", exc)
        return {"synced": 0, "error": str(exc)}
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
    return {"synced": count}


def _asset_id_from_ip(ip: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", ip)
    return f"disc-{safe}"
