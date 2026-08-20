from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Asset, EscalationPolicy, Incident, Playbook, Playrule, User
from app.security import hash_password
from app.settings import settings

DEMO_ASSET = "forge-demo-01"
DEMO_OWNER = "platform"
DEMO_CONTACT_NAME = "Platform on-call"
DEMO_OWNER_EMAIL = "platform@forgesre.local"
DEMO_OWNER_PHONE = "+381-11-000-0000"
DEMO_NOTES = "Seeded demo host. Not a real machine. Used by ./forgesre demo."

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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_demo_asset(db: Session) -> Asset:
    asset = db.query(Asset).filter_by(asset_id=DEMO_ASSET).first()
    if asset is None:
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
    count = db.query(func.count(Incident.id)).scalar() or 0
    row = Incident(
        number=f"INC-{count + 1:06d}",
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

    asset = ensure_demo_asset(db)
    ensure_demo_similar_history(db, asset)

    from app.inventory import seed_demo_candidate

    seed_demo_candidate(db)
    db.commit()
