from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base

JSONType = JSON

# Must match services.tracked_incident_statuses() (paging + quiet MAINT/SUPPRESSED).
TRACKED_INCIDENT_STATUSES = ("OPEN", "INVESTIGATING", "ESCALATED", "MAINT", "SUPPRESSED")
_TRACKED_FINGERPRINT_SQL = ", ".join(f"'{status}'" for status in TRACKED_INCIDENT_STATUSES)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email2: Mapped[str] = mapped_column(String(255), default="")
    name: Mapped[str] = mapped_column(String(255))
    surname: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    phone2: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    journal_error_ack_id: Mapped[int] = mapped_column(Integer, default=0)
    # Bumped on logout / password change; session cookies carry it and must match.
    session_version: Mapped[int] = mapped_column(Integer, default=0)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
    asset_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(64), default="Linux Server")
    environment: Mapped[str] = mapped_column(String(64), default="Production")
    status: Mapped[str] = mapped_column(String(32), default="healthy")
    monitoring_profile: Mapped[str] = mapped_column(String(64), default="linux-standard")
    owner: Mapped[str] = mapped_column(String(255), default="platform")
    contact_name: Mapped[str] = mapped_column(String(255), default="")
    owner_email: Mapped[str] = mapped_column(String(255), default="")
    owner_phone: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    netbox_id: Mapped[str] = mapped_column(String(64), default="")
    zabbix_hostid: Mapped[str] = mapped_column(String(64), default="", index=True)
    zabbix_agent: Mapped[str] = mapped_column(String(16), default="")
    glpi_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    scrape_address: Mapped[str] = mapped_column(String(128), default="")
    snmp_port: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    alarms: Mapped[dict] = mapped_column(JSONType, default=dict)
    extras: Mapped[dict | None] = mapped_column(JSONType, default=dict, nullable=True)
    playrule_ids: Mapped[list | None] = mapped_column(JSONType, default=list, nullable=True)
    ping_status: Mapped[str] = mapped_column(String(16), default="yellow")
    ping_detail: Mapped[str] = mapped_column(String(255), default="")
    exporter_status: Mapped[str] = mapped_column(String(16), default="yellow")
    exporter_detail: Mapped[str] = mapped_column(String(255), default="")
    probe_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True, index=True)

    incidents: Mapped[list[Incident]] = relationship(back_populates="asset")
    parent: Mapped["Asset | None"] = relationship("Asset", remote_side="Asset.id", foreign_keys="Asset.parent_id")
    ladder_steps: Mapped[list["AssetLadderStep"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetLadderStep.level",
    )
    playrule_mutes: Mapped[list["AssetPlayruleMute"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )


class AssetLadderStep(Base):
    """One mail level on an asset. Empty table for that asset means the global Default ladder."""

    __tablename__ = "asset_ladder_steps"
    __table_args__ = (UniqueConstraint("asset_id", "level", name="uq_asset_ladder_level"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    after_minutes: Mapped[int] = mapped_column(Integer, default=0)
    emails: Mapped[list] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    asset: Mapped[Asset] = relationship(back_populates="ladder_steps")


class AssetPlayruleMute(Base):
    """Custom alarm OFF for one asset and one playrule.

    A row means this host skips that playrule on ingest. No row means ON
    (the asset's custom alarms are tried first, as before). The id stays in
    assets.playrule_ids so the operator can turn it back ON.
    """

    __tablename__ = "asset_playrule_mutes"
    __table_args__ = (UniqueConstraint("asset_id", "playrule_id", name="uq_asset_playrule_mute"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    playrule_id: Mapped[int] = mapped_column(Integer, index=True)

    asset: Mapped[Asset] = relationship(back_populates="playrule_mutes")


class AssetNumberSeq(Base):
    """High-water mark for Asset.number. Never decreases after delete."""

    __tablename__ = "asset_number_seq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last: Mapped[int] = mapped_column(Integer, default=0)


@event.listens_for(Session, "before_flush")
def _assign_asset_numbers(session: Session, flush_context, instances) -> None:
    """Stable inventory #. Auto-increment; never reuse after delete (gaps are OK)."""
    del flush_context, instances
    pending = [obj for obj in session.new if isinstance(obj, Asset) and not getattr(obj, "number", None)]
    if not pending:
        return
    seq = session.get(AssetNumberSeq, 1)
    if seq is None:
        for obj in session.new:
            if isinstance(obj, AssetNumberSeq) and obj.id == 1:
                seq = obj
                break
    if seq is None:
        live = session.query(func.max(Asset.number)).scalar()
        seq = AssetNumberSeq(id=1, last=int(live or 0))
        session.add(seq)
    taken = {
        int(obj.number)
        for obj in list(session.new) + list(session.dirty)
        if isinstance(obj, Asset) and getattr(obj, "number", None)
    }
    n = int(seq.last or 0)
    for asset in pending:
        n += 1
        while n in taken:
            n += 1
        asset.number = n
        taken.add(n)
    seq.last = n


class Playbook(Base):
    __tablename__ = "playbooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSONType, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EscalationPolicy(Base):
    __tablename__ = "escalation_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    steps: Mapped[list] = mapped_column(JSONType, default=list)


class Playrule(Base):
    __tablename__ = "playrules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    severity: Mapped[str] = mapped_column(String(32), default="warning")
    condition: Mapped[dict] = mapped_column(JSONType, default=dict)
    playbook_id: Mapped[int | None] = mapped_column(ForeignKey("playbooks.id"), nullable=True)
    escalation_policy_id: Mapped[int | None] = mapped_column(ForeignKey("escalation_policies.id"), nullable=True)

    playbook: Mapped[Playbook | None] = relationship()
    escalation_policy: Mapped[EscalationPolicy | None] = relationship()


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index(
            "ux_incidents_active_fingerprint",
            "fingerprint",
            unique=True,
            sqlite_where=text(f"status IN ({_TRACKED_FINGERPRINT_SQL})"),
            postgresql_where=text(f"status IN ({_TRACKED_FINGERPRINT_SQL})"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    severity: Mapped[str] = mapped_column(String(32), default="WARNING")
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    fingerprint: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32), default="prometheus")
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True, index=True)
    playrule_id: Mapped[int | None] = mapped_column(ForeignKey("playrules.id"), nullable=True)
    playbook_id: Mapped[int | None] = mapped_column(ForeignKey("playbooks.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_by: Mapped[str] = mapped_column(String(255), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    timeline: Mapped[list] = mapped_column(JSONType, default=list)
    alert_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    # Playbook steps the operator ticked on this incident (indexes into playbook.steps).
    checklist: Mapped[list] = mapped_column(JSONType, default=list)
    # Analyst review: the confirmed cause and how ForgeRCA / ForgeAI did (correct, partly, wrong).
    root_cause: Mapped[str] = mapped_column(Text, default="")
    rca_rating: Mapped[str] = mapped_column(String(16), default="")
    ai_rating: Mapped[str] = mapped_column(String(16), default="")
    reviewed_by: Mapped[str] = mapped_column(String(255), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    asset: Mapped[Asset | None] = relationship(back_populates="incidents")
    playrule: Mapped[Playrule | None] = relationship()
    playbook: Mapped[Playbook | None] = relationship()
    evidence: Mapped[list[Evidence]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    investigations: Mapped[list[Investigation]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    events: Mapped[list[IncidentEvent]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    operator_notes: Mapped[list["IncidentNote"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evidence_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    source: Mapped[str] = mapped_column(String(64), default="")
    query: Mapped[str] = mapped_column(Text, default="")
    asset_ref: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    likely_cause: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    evidence: Mapped[list] = mapped_column(JSONType, default=list)
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(64), default="builtin-analyst")
    disclaimer: Mapped[str] = mapped_column(Text, default="AI has not modified the system.")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    engine: Mapped[str] = mapped_column(String(64), default="forgerca")
    engine_version: Mapped[str] = mapped_column(String(32), default="0.3.0")
    model: Mapped[str] = mapped_column(String(128), default="")
    requested_by: Mapped[str] = mapped_column(String(255), default="")

    incident: Mapped[Incident] = relationship(back_populates="investigations")


class ForgeAiSkill(Base):
    """One ForgeAI prompt for one kind of work (task) and one kind of incident (match).

    ``instructions`` is the editable part; the fixed safety rules and the JSON shape of the task
    are added by app.forgeai and cannot be edited. Built-in skills keep ``default_instructions``
    so "Reset to default" works.
    """

    __tablename__ = "forgeai_skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    task: Mapped[str] = mapped_column(String(32), default="incident", index=True)
    match_alerts: Mapped[list] = mapped_column(JSONType, default=list)
    match_types: Mapped[list] = mapped_column(JSONType, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    instructions: Mapped[str] = mapped_column(Text, default="")
    default_instructions: Mapped[str] = mapped_column(Text, default="")
    include: Mapped[dict] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list] = mapped_column(JSONType, default=list)
    updated_by: Mapped[str] = mapped_column(String(255), default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ForgeAiCall(Base):
    """Every question ForgeSRE put to the language model: exact prompt, raw answer, outcome, time.

    ``status``: used (shown to operators), rejected (failed a check, see ``reason``), error (no
    answer), trial (a skill test; never shown as the incident's answer).
    """

    __tablename__ = "forgeai_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True, index=True)
    task: Mapped[str] = mapped_column(String(32), default="incident", index=True)
    skill_slug: Mapped[str] = mapped_column(String(64), default="", index=True)
    skill_version: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(128), default="")
    endpoint: Mapped[str] = mapped_column(String(255), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    user_prompt: Mapped[str] = mapped_column(Text, default="")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="error", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(255), default="system")
    rating: Mapped[int] = mapped_column(Integer, default=0)
    rated_by: Mapped[str] = mapped_column(String(255), default="")


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    kind: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONType, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="events")


class IncidentNote(Base):
    """Short operator comment on an incident. Not a ticketing thread."""

    __tablename__ = "incident_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")

    incident: Mapped[Incident] = relationship(back_populates="operator_notes")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    target: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="generated")
    step_key: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MailContact(Base):
    """Address-book entry for Email & reports. Not an inbox."""

    __tablename__ = "mail_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduledReport(Base):
    """Periodic performance email. Not tied to an incident."""

    __tablename__ = "scheduled_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), default="performance", index=True)
    to_email: Mapped[str] = mapped_column(String(255))
    recipients: Mapped[list] = mapped_column(JSONType, default=list)
    interval_hours: Mapped[int] = mapped_column(Integer, default=6)
    schedule: Mapped[dict] = mapped_column(JSONType, default=dict)
    asset_ids: Mapped[list] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str] = mapped_column(String(64), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[dict] = mapped_column(JSONType, default=dict)


class JournalEntry(Base):
    """Per-module process report. Not the user audit log; not a raw log dump."""

    __tablename__ = "journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    summary: Mapped[str] = mapped_column(String(512), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    object_type: Mapped[str] = mapped_column(String(64), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    proposed_role: Mapped[str] = mapped_column(String(128), default="Unknown device")
    open_ports: Mapped[list] = mapped_column(JSONType, default=list)
    snmp_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    node_exporter: Mapped[bool] = mapped_column(Boolean, default=False)
    windows_exporter: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    source: Mapped[str] = mapped_column(String(32), default="scan")
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str] = mapped_column(String(255), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    asset_id: Mapped[str] = mapped_column(String(64), default="")


class AssetIdentity(Base):
    """What one outside source (GLPI, Zabbix, NetBox, LibreNMS) says about one asset.

    Used to match the next sync by serial, MAC, name or IP, and to know which values a source
    wrote (``fields``), so it may update them later without overwriting a person's edit.
    """

    __tablename__ = "asset_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(128), default="", index=True)
    mac: Mapped[str] = mapped_column(String(64), default="", index=True)
    fields: Mapped[dict] = mapped_column(JSONType, default=dict)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetConflict(Base):
    """A source disagrees with the inventory. Never applied silently; an Engineer keeps ours or takes theirs."""

    __tablename__ = "asset_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32))  # field, ip, match
    field: Mapped[str] = mapped_column(String(64), default="")
    ours: Mapped[str] = mapped_column(Text, default="")
    theirs: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open, ours, theirs
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str] = mapped_column(String(255), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopBeat(Base):
    """Last time a background loop ran, and in which process. Shared by every Core and worker process."""

    __tablename__ = "loop_beats"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    process: Mapped[str] = mapped_column(String(128), default="")
    leader: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginFailure(Base):
    """One failed sign-in, kept for the throttle window. Shared by every Core process."""

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(320), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Job(Base):
    """Background work (RCA, discovery scan). Not a message broker — one table, one worker thread."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    object_type: Mapped[str] = mapped_column(String(64), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OncallSlot(Base):
    """Weekly on-call window. weekday is Monday=0 … Sunday=6, minutes are local to the appliance timezone."""

    __tablename__ = "oncall_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer, index=True)
    start_minute: Mapped[int] = mapped_column(Integer, default=0)
    end_minute: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(255), default="")
    surname: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    phone2: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Slo(Base):
    """Availability objective: e.g. 99.9 % of the last 30 days without a critical incident.

    Scope works like noise filters: all, one asset, an asset type or a site. For a group the
    downtime is the union of its incidents (the service is down while any member is).
    """

    __tablename__ = "slos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    scope: Mapped[str] = mapped_column(String(16), default="all")
    scope_value: Mapped[str] = mapped_column(String(255), default="")
    target: Mapped[float] = mapped_column(Float, default=99.9)
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    severities: Mapped[list] = mapped_column(JSONType, default=lambda: ["CRITICAL"])
    owner: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckResult(Base):
    """Latest result of one appliance check (TLS certificate, ForgeSRE backup, heartbeat).

    One row per (kind, target), overwritten on every run. ``status`` is ok | warning | critical
    | error; alarms go through the normal alert path, this row is what the UI and CLI show.
    """

    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    target: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[str] = mapped_column(String(500), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Heartbeat(Base):
    """Dead-man switch: a backup or cron job pings after each success; silence past ``every_hours`` alarms."""

    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    asset_ref: Mapped[str] = mapped_column(String(255), default="")
    every_hours: Mapped[float] = mapped_column(Float, default=24.0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeEvent(Base):
    """Something that changed outside ForgeSRE: a deploy, a config push, a GLPI change ticket.

    Posted by CI/CD or scripts to POST /api/v1/changes (bearer CHANGES_WEBHOOK_TOKEN) or added
    by a person in the UI. Incidents show the changes on their asset, its parent, or global
    ones (no asset) from the hours before they started.
    """

    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    kind: Mapped[str] = mapped_column(String(32), default="change")
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    actor: Mapped[str] = mapped_column(String(255), default="")
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True)


class TopologyLink(Base):
    """One LLDP/CDP neighbour seen by LibreNMS, matched to our assets where possible.

    Rows are replaced on every sync from the same source. They feed parent suggestions; the
    parent pointer on Asset stays the one source of truth for parent-down suppression.
    """

    __tablename__ = "topology_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="librenms", index=True)
    local_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=True, index=True)
    remote_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=True, index=True)
    local_name: Mapped[str] = mapped_column(String(255), default="")
    remote_name: Mapped[str] = mapped_column(String(255), default="")
    local_port: Mapped[str] = mapped_column(String(128), default="")
    remote_port: Mapped[str] = mapped_column(String(128), default="")
    protocol: Mapped[str] = mapped_column(String(16), default="")
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NoiseFilter(Base):
    """Known noise: an alarm that matches is recorded but nobody is notified while it applies.

    See docs/alerting.md and app/noise_filters.py. Window minutes are in the appliance
    time zone; end < start means the window crosses midnight.
    """

    __tablename__ = "noise_filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    alertnames: Mapped[list] = mapped_column(JSONType, default=list)  # empty = any alert
    scope: Mapped[str] = mapped_column(String(16), default="all")  # all | asset | type | site
    scope_value: Mapped[str] = mapped_column(String(255), default="")
    days: Mapped[list] = mapped_column(JSONType, default=list)  # weekdays 0=Mon..6; empty = every day
    start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # only below this sample
    action: Mapped[str] = mapped_column(String(16), default="quiet")  # quiet | delay
    delay_minutes: Mapped[int] = mapped_column(Integer, default=5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[str] = mapped_column(String(255), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_ref: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


@event.listens_for(Asset, "after_insert")
@event.listens_for(Asset, "after_update")
@event.listens_for(Asset, "after_delete")
def _asset_changed_forget_print_verdict(_mapper, _connection, target: Asset) -> None:
    """A new or changed asset type decides again whether its TCP 9100 may be probed (printer guard)."""
    from app.printer_guard import forget

    for name in (target.ip, target.hostname):
        if name:
            forget(name)
