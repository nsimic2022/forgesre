from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.demo_ids import DEMO_ASSET_PREFIX, is_demo_asset_id
from app.models import AuditLog, Asset, EscalationPolicy, Incident, Playbook, Playrule, User, utcnow
from app.security import hash_password
from app.settings import settings

DEMO_ASSET = "forge-demo-01"
DEMO_WIN_ASSET = "forge-demo-win-01"
DEMO_SW_ASSET = "forge-demo-sw-01"
DEMO_OWNER = "platform"
DEMO_CONTACT_NAME = "Platform on-call"
DEMO_OWNER_EMAIL = "platform@forgesre.local"
DEMO_OWNER_PHONE = "+381-11-000-0000"
DEMO_NOTES = "Seeded demo host. Not a real machine. Used by ./forgesre demo."
DEMO_WIN_NOTES = (
    "Seeded demo Windows lab host. Not a real machine. "
    "Lab incidents only — this row is not scraped. Real Windows assets use windows_exporter :9182. "
    "Used by Dashboard → Run demo."
)
DEMO_SW_NOTES = (
    "Seeded demo network lab device. Not a real switch. "
    "Lab incidents only — not a live SNMP walk. Used by Dashboard → Run demo."
)


DISK_STEPS = [
    {"id": "verify", "title": "Verify disk usage"},
    {"id": "growth", "title": "Check growth"},
    {"id": "owner", "title": "Identify owner"},
    {"id": "notify", "title": "Notify responsible engineer"},
    {"id": "escalate", "title": "Escalate if not acknowledged"},
]

CPU_STEPS = [
    {"id": "verify", "title": "Verify CPU usage"},
    {"id": "process", "title": "Identify top CPU processes"},
    {"id": "owner", "title": "Identify owner"},
    {"id": "notify", "title": "Notify responsible engineer"},
    {"id": "escalate", "title": "Escalate if not acknowledged"},
]

MEMORY_STEPS = [
    {"id": "verify", "title": "Verify memory usage (available vs total)"},
    {"id": "process", "title": "Identify top memory consumers"},
    {"id": "owner", "title": "Identify owner"},
    {"id": "notify", "title": "Notify responsible engineer"},
    {"id": "escalate", "title": "Escalate if not acknowledged"},
]

HOST_STEPS = [
    {"id": "verify", "title": "Verify the host answers ping / SSH from the management network"},
    {"id": "exporter", "title": "Check node_exporter on TCP/9100 from the ForgeSRE host"},
    {"id": "owner", "title": "Identify owner"},
    {"id": "notify", "title": "Notify responsible engineer"},
    {"id": "escalate", "title": "Escalate if not acknowledged"},
]

WINDOWS_STEPS = [
    {"id": "verify", "title": "Verify the host answers ping / RDP from the management network"},
    {"id": "exporter", "title": "Check windows_exporter on TCP/9182 from the ForgeSRE host (curl http://<ip>:9182/metrics)"},
    {"id": "owner", "title": "Identify owner"},
    {"id": "notify", "title": "Notify responsible engineer"},
    {"id": "escalate", "title": "Escalate if not acknowledged"},
]

NETWORK_STEPS = [
    {"id": "verify", "title": "Verify the device answers ping / SSH / console"},
    {"id": "snmp", "title": "Check SNMP community, ACL, and UDP/161 from the ForgeSRE host"},
    {"id": "owner", "title": "Identify owner"},
    {"id": "notify", "title": "Notify responsible engineer"},
    {"id": "escalate", "title": "Escalate if not acknowledged"},
]


def _demo_deleted_by_operator(db: Session, asset_id: str) -> bool:
    """True after GUI/CLI Remove. Seed must not put forge-demo-* back on Core start."""
    return (
        db.query(AuditLog.id)
        .filter(AuditLog.action == "asset.delete", AuditLog.object_id == asset_id)
        .first()
        is not None
    )


def ensure_demo_asset(db: Session) -> Asset | None:
    asset = db.query(Asset).filter_by(asset_id=DEMO_ASSET).first()
    if asset is None:
        if _demo_deleted_by_operator(db, DEMO_ASSET):
            return None
        asset = Asset(
            asset_id=DEMO_ASSET,
            hostname=DEMO_ASSET,
            ip="10.10.10.20",
            type="Linux Server",
            environment="Production",
            status="healthy",
            monitoring_profile="linux-standard",
            owner=DEMO_OWNER,
            contact_name=DEMO_CONTACT_NAME,
            owner_email=DEMO_OWNER_EMAIL,
            owner_phone=DEMO_OWNER_PHONE,
            notes=DEMO_NOTES,
            source="manual",
            scrape_address="",
        )
        db.add(asset)
        db.flush()
        return asset
    if not (asset.owner_email or "").strip():
        asset.owner = DEMO_OWNER
        asset.contact_name = DEMO_CONTACT_NAME
        asset.owner_email = DEMO_OWNER_EMAIL
        asset.owner_phone = DEMO_OWNER_PHONE
    if not (asset.notes or "").strip():
        asset.notes = DEMO_NOTES
    return asset


def _ensure_lab_asset(
    db: Session,
    *,
    asset_id: str,
    hostname: str,
    ip: str,
    type: str,
    monitoring_profile: str,
    notes: str,
) -> Asset | None:
    asset = db.query(Asset).filter_by(asset_id=asset_id).first()
    if asset is None:
        if _demo_deleted_by_operator(db, asset_id):
            return None
        asset = Asset(
            asset_id=asset_id,
            hostname=hostname,
            ip=ip,
            type=type,
            environment="Production",
            status="healthy",
            monitoring_profile=monitoring_profile,
            owner=DEMO_OWNER,
            contact_name=DEMO_CONTACT_NAME,
            owner_email=DEMO_OWNER_EMAIL,
            owner_phone=DEMO_OWNER_PHONE,
            notes=notes,
            source="manual",
            scrape_address="",
        )
        db.add(asset)
        db.flush()
        return asset
    asset.hostname = hostname
    asset.type = type
    asset.monitoring_profile = monitoring_profile
    asset.scrape_address = ""
    if not (asset.ip or "").strip():
        asset.ip = ip
    if not (asset.owner_email or "").strip():
        asset.owner = DEMO_OWNER
        asset.contact_name = DEMO_CONTACT_NAME
        asset.owner_email = DEMO_OWNER_EMAIL
        asset.owner_phone = DEMO_OWNER_PHONE
    if not (asset.notes or "").strip():
        asset.notes = notes
    return asset


def ensure_demo_windows_asset(db: Session) -> Asset | None:
    return _ensure_lab_asset(
        db,
        asset_id=DEMO_WIN_ASSET,
        hostname=DEMO_WIN_ASSET,
        ip="10.10.10.21",
        type="Windows Server",
        monitoring_profile="windows-lab",
        notes=DEMO_WIN_NOTES,
    )


def ensure_demo_switch_asset(db: Session) -> Asset | None:
    return _ensure_lab_asset(
        db,
        asset_id=DEMO_SW_ASSET,
        hostname=DEMO_SW_ASSET,
        ip="10.10.10.22",
        type="Network Switch",
        monitoring_profile="network-lab",
        notes=DEMO_SW_NOTES,
    )


def ensure_demo_similar_history(db: Session, asset: Asset) -> Incident:
    fingerprint = f"HighCPU:{DEMO_ASSET}"
    existing = (
        db.query(Incident)
        .filter(
            Incident.asset_id == asset.id,
            Incident.status == "CLOSED",
            Incident.fingerprint == fingerprint,
        )
        .first()
    )
    if existing:
        return existing
    rule = db.query(Playrule).filter_by(name="high-cpu").first()
    started = utcnow() - timedelta(days=7)
    ended = started + timedelta(hours=2)
    from app.services import next_incident_number

    row = Incident(
        number=next_incident_number(db),
        title="High CPU",
        severity="WARNING",
        status="CLOSED",
        fingerprint=fingerprint,
        asset_id=asset.id,
        playrule_id=rule.id if rule else None,
        playbook_id=rule.playbook_id if rule else None,
        started_at=started,
        ended_at=ended,
        summary="Previous HighCPU on forge-demo-01 (seeded demo history). CPU returned to normal.",
        alert_payload={
            "labels": {"alertname": "HighCPU", "severity": "warning", "asset": DEMO_ASSET},
            "annotations": {
                "summary": "High CPU",
                "description": "Seeded similar-incident history so the asset page has something to show after install.",
            },
        },
        timeline=[
            {"id": "alert", "title": "ALERT", "detail": "HighCPU fired (demo history)", "at": started.isoformat()},
            {
                "id": "closed",
                "title": "CLOSED",
                "detail": "Resolved in the lab demo history.",
                "at": ended.isoformat(),
            },
        ],
    )
    db.add(row)
    db.flush()
    return row


def seed(db: Session) -> None:
    if db.query(User).filter_by(email=settings.admin_email).first() is None:
        db.add(
            User(
                email=settings.admin_email,
                name="Super Admin",
                password_hash=hash_password(settings.admin_password),
                role="super_admin",
            )
        )

    disk = db.query(Playbook).filter_by(slug="disk-full").first()
    if disk is None:
        disk = Playbook(
            slug="disk-full",
            name="DISK-FULL",
            description="High filesystem usage workflow. Guidance only — no commands are executed.",
            steps=DISK_STEPS,
        )
        db.add(disk)
        db.flush()

    cpu = db.query(Playbook).filter_by(slug="cpu-high").first()
    if cpu is None:
        cpu = Playbook(
            slug="cpu-high",
            name="CPU-HIGH",
            description="High CPU workflow. Guidance only — no commands are executed.",
            steps=CPU_STEPS,
        )
        db.add(cpu)
        db.flush()

    mem = db.query(Playbook).filter_by(slug="memory-high").first()
    if mem is None:
        mem = Playbook(
            slug="memory-high",
            name="MEMORY-HIGH",
            description="High memory usage workflow. Guidance only — no commands are executed.",
            steps=MEMORY_STEPS,
        )
        db.add(mem)
        db.flush()

    net = db.query(Playbook).filter_by(slug="network-unreachable").first()
    if net is None:
        net = Playbook(
            slug="network-unreachable",
            name="NETWORK-UNREACHABLE",
            description="SNMP scrape failed. Guidance only — no commands are executed.",
            steps=NETWORK_STEPS,
        )
        db.add(net)
        db.flush()

    policy = db.query(EscalationPolicy).filter_by(slug="default-warning").first()
    if policy is None:
        policy = EscalationPolicy(
            slug="default-warning",
            name="Default warning",
            steps=[
                {"after_minutes": 0, "target": "team", "channel": "email"},
                {"after_minutes": 15, "target": "team-lead", "channel": "email"},
                {"after_minutes": 30, "target": "engineer", "channel": "email"},
            ],
        )
        db.add(policy)
        db.flush()

    if db.query(Playrule).filter_by(name="high-cpu").first() is None:
        db.add(
            Playrule(
                name="high-cpu",
                description="CPU usage above 80%",
                enabled=True,
                severity="warning",
                condition={"alertname": "HighCPU", "metric": "cpu_usage", "operator": ">", "value": 80},
                playbook_id=cpu.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="high-disk").first() is None:
        db.add(
            Playrule(
                name="high-disk",
                description="Filesystem usage above 80%",
                enabled=True,
                severity="warning",
                condition={
                    "alertname": "FilesystemUsageHigh",
                    "metric": "filesystem_usage",
                    "operator": ">",
                    "value": 80,
                },
                playbook_id=disk.id,
                escalation_policy_id=policy.id,
            )
        )

    host = db.query(Playbook).filter_by(slug="host-unreachable").first()
    if host is None:
        host = Playbook(
            slug="host-unreachable",
            name="HOST-UNREACHABLE",
            description="node_exporter scrape failed. Guidance only — no commands are executed.",
            steps=HOST_STEPS,
        )
        db.add(host)
        db.flush()

    if db.query(Playrule).filter_by(name="snmp-down").first() is None:
        db.add(
            Playrule(
                name="snmp-down",
                description="SNMP scrape failed (device down or community/ACL)",
                enabled=True,
                severity="warning",
                condition={"alertname": "SnmpDeviceUnreachable", "metric": "up", "operator": "==", "value": 0},
                playbook_id=net.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="node-exporter-down").first() is None:
        db.add(
            Playrule(
                name="node-exporter-down",
                description="node_exporter scrape failed",
                enabled=True,
                severity="warning",
                condition={"alertname": "NodeExporterDown", "metric": "up", "operator": "==", "value": 0},
                playbook_id=host.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="node-filesystem").first() is None:
        db.add(
            Playrule(
                name="node-filesystem",
                description="node_exporter filesystem usage above 90%",
                enabled=True,
                severity="warning",
                condition={"alertname": "NodeFilesystemUsageHigh", "metric": "filesystem_usage", "operator": ">", "value": 90},
                playbook_id=disk.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="network-if-down").first() is None:
        db.add(
            Playrule(
                name="network-if-down",
                description="Interface oper-down while admin-up (if_mib)",
                enabled=True,
                severity="warning",
                condition={"alertname": "NetworkInterfaceDown", "metric": "ifOperStatus", "operator": "==", "value": 2},
                playbook_id=net.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="node-cpu").first() is None:
        db.add(
            Playrule(
                name="node-cpu",
                description="node_exporter CPU above 95%",
                enabled=True,
                severity="warning",
                condition={"alertname": "NodeCPUHigh", "metric": "cpu_usage", "operator": ">", "value": 95},
                playbook_id=cpu.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="node-memory").first() is None:
        db.add(
            Playrule(
                name="node-memory",
                description="node_exporter memory above 90%",
                enabled=True,
                severity="warning",
                condition={"alertname": "NodeMemoryHigh", "metric": "memory_usage", "operator": ">", "value": 90},
                playbook_id=mem.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="windows-cpu").first() is None:
        db.add(
            Playrule(
                name="windows-cpu",
                description="windows_exporter CPU above 90% (also matches the lab WindowsCPUHigh demo)",
                enabled=True,
                severity="warning",
                condition={"alertname": "WindowsCPUHigh", "metric": "cpu_usage", "operator": ">", "value": 90},
                playbook_id=cpu.id,
                escalation_policy_id=policy.id,
            )
        )
    else:
        existing_win = db.query(Playrule).filter_by(name="windows-cpu").first()
        if existing_win and "no windows_exporter" in (existing_win.description or ""):
            existing_win.description = "windows_exporter CPU above 90% (also matches the lab WindowsCPUHigh demo)"

    win_host = db.query(Playbook).filter_by(slug="windows-unreachable").first()
    if win_host is None:
        win_host = Playbook(
            slug="windows-unreachable",
            name="WINDOWS-UNREACHABLE",
            description="windows_exporter scrape failed. Guidance only — no commands are executed.",
            steps=WINDOWS_STEPS,
        )
        db.add(win_host)
        db.flush()

    if db.query(Playrule).filter_by(name="windows-exporter-down").first() is None:
        db.add(
            Playrule(
                name="windows-exporter-down",
                description="windows_exporter scrape failed",
                enabled=True,
                severity="warning",
                condition={"alertname": "WindowsExporterDown", "metric": "up", "operator": "==", "value": 0},
                playbook_id=win_host.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="windows-filesystem").first() is None:
        db.add(
            Playrule(
                name="windows-filesystem",
                description="windows_exporter volume usage above 90%",
                enabled=True,
                severity="warning",
                condition={"alertname": "WindowsFilesystemUsageHigh", "metric": "filesystem_usage", "operator": ">", "value": 90},
                playbook_id=disk.id,
                escalation_policy_id=policy.id,
            )
        )
    if db.query(Playrule).filter_by(name="windows-memory").first() is None:
        db.add(
            Playrule(
                name="windows-memory",
                description="windows_exporter memory above 90%",
                enabled=True,
                severity="warning",
                condition={"alertname": "WindowsMemoryHigh", "metric": "memory_usage", "operator": ">", "value": 90},
                playbook_id=mem.id,
                escalation_policy_id=policy.id,
            )
        )

    asset = ensure_demo_asset(db)
    if asset is not None:
        ensure_demo_similar_history(db, asset)
    ensure_demo_windows_asset(db)
    ensure_demo_switch_asset(db)

    from app.inventory import seed_demo_candidate

    seed_demo_candidate(db)
    db.commit()
