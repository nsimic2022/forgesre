from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Asset, EscalationPolicy, Playbook, Playrule, User
from app.security import hash_password
from app.settings import settings

DEMO_ASSET = "forge-demo-01"

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

    if db.query(Asset).filter_by(asset_id=DEMO_ASSET).first() is None:
        db.add(
            Asset(
                asset_id=DEMO_ASSET,
                hostname=DEMO_ASSET,
                ip="10.10.10.20",
                type="Linux Server",
                environment="Production",
                status="healthy",
                monitoring_profile="linux-standard",
                owner="platform",
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
    db.commit()
