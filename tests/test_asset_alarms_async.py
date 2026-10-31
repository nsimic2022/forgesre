"""Async alarm state: IP fetch, DOWN/UP, resolve by fingerprint, downtime for SLA."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.asset_alarms import (
    _resolve_active_incident,
    canonical_asset_status,
    process_incoming_alarm_state,
    process_incoming_alarm_states,
)
from app.asset_extras import OUTAGES_KEY, merge_extras
from app.asset_metrics import record_outage, sla_percent
from app.db import get_db_session
from app.inventory import create_manual_asset
from app.models import AuditLog, Incident, IncidentNote
from app.seed import seed
from app.services import ingest_alertmanager


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    n = uuid4().int
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0x7F or 1}"


def _alert(ip: str, asset_id: str, status: str, name: str = "SnmpDeviceUnreachable") -> dict:
    return {
        "status": status,
        "labels": {
            "alertname": name,
            "asset": asset_id,
            "instance": f"{ip}:161",
            "ip": ip,
            "severity": "critical",
        },
        "annotations": {"summary": name},
    }


def test_canonical_status_maps_down_up():
    assert canonical_asset_status("DOWN") == "critical"
    assert canonical_asset_status("UP") == "healthy"
    assert canonical_asset_status("warning") == "warning"


def test_record_outage_and_sla():
    extras = record_outage(
        {},
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        seconds=3600,
        fingerprint="SnmpDeviceUnreachable:sw-1",
    )
    assert extras[OUTAGES_KEY][0]["seconds"] == 3600
    assert sla_percent(extras, window_seconds=36000) == 90.0
    assert sla_percent({}) is None
    kept = merge_extras({**extras, "site": "DC1"}, {"site": "DC2"}, None)
    assert kept["site"] == "DC2"
    assert kept[OUTAGES_KEY][0]["fingerprint"] == "SnmpDeviceUnreachable:sw-1"


def test_process_firing_marks_asset_down():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"alarm-down-{uuid4().hex[:6]}", ip=ip, type="Network device", actor="tester"
    )
    assert host.status == "healthy"

    async def _run():
        async with get_db_session() as session:
            return await process_incoming_alarm_state(session, _alert(ip, host.asset_id, "firing"))

    result = asyncio.run(_run())
    db.expire_all()
    db.refresh(host)
    assert result["status"] == "firing"
    assert host.status == "critical"
    db.close()


def test_resolve_active_incident_by_fingerprint():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"alarm-up-{uuid4().hex[:6]}", ip=ip, type="Network device", actor="tester"
    )
    started = datetime.now(timezone.utc) - timedelta(minutes=12)
    fingerprint = f"SnmpDeviceUnreachable:{host.asset_id}"
    incident = Incident(
        number=f"INC-AL-{uuid4().hex[:6]}",
        title="box down",
        severity="CRITICAL",
        status="OPEN",
        fingerprint=fingerprint,
        source="snmp",
        asset_id=host.id,
        started_at=started,
        timeline=[],
        alert_payload={},
    )
    db.add(incident)
    host.status = "critical"
    db.commit()
    db.refresh(incident)
    db.refresh(host)

    async def _run():
        async with get_db_session() as session:
            return await _resolve_active_incident(
                session,
                await session.get(type(host), host.id),
                fingerprint,
                alertname="SnmpDeviceUnreachable",
                resolver="LibreNMS",
                labels={"alertname": "SnmpDeviceUnreachable"},
            )

    closed = asyncio.run(_run())
    db.expire_all()
    db.refresh(incident)
    db.refresh(host)
    assert closed is not None
    assert incident.status == "RESOLVED"
    assert incident.ended_at is not None
    assert incident.resolved_at is not None
    notes = db.query(IncidentNote).filter_by(incident_id=incident.id).all()
    assert any("Automatic close" in (row.body or "") for row in notes)
    audits = db.query(AuditLog).filter_by(action="incident.resolve", object_id=incident.number).all()
    assert audits
    assert host.status == "healthy"
    history = (host.extras or {}).get(OUTAGES_KEY) or []
    assert history and history[-1]["seconds"] >= 60
    db.close()


def test_process_resolved_unknown_fingerprint_is_safe():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"alarm-miss-{uuid4().hex[:6]}", ip=ip, type="Network device", actor="tester"
    )

    async def _run():
        async with get_db_session() as session:
            return await process_incoming_alarm_state(session, _alert(ip, host.asset_id, "resolved"))

    result = asyncio.run(_run())
    assert result["status"] == "resolved-miss"
    db.close()


def test_process_states_batches_two_assets():
    db = _db()
    first_ip, second_ip = _ip(), _ip()
    first = create_manual_asset(
        db, hostname=f"alarm-a-{uuid4().hex[:6]}", ip=first_ip, type="Network device", actor="tester"
    )
    second = create_manual_asset(
        db, hostname=f"alarm-b-{uuid4().hex[:6]}", ip=second_ip, type="Network device", actor="tester"
    )

    async def _run():
        async with get_db_session() as session:
            return await process_incoming_alarm_states(
                session,
                [
                    _alert(first_ip, first.asset_id, "firing"),
                    _alert(second_ip, second.asset_id, "firing"),
                ],
            )

    results = asyncio.run(_run())
    assert [row["status"] for row in results] == ["firing", "firing"]
    db.expire_all()
    db.refresh(first)
    db.refresh(second)
    assert first.status == "critical" and second.status == "critical"
    db.close()


def test_ingest_resolve_records_downtime():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"alarm-ing-{uuid4().hex[:6]}", ip=ip, type="Linux Server", actor="tester"
    )
    name = "NodeExporterDown"
    opened = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [_alert(ip, host.asset_id, "firing", name)],
        },
    )
    assert len(opened) == 1
    ingest_alertmanager(
        db,
        {
            "status": "resolved",
            "alerts": [_alert(ip, host.asset_id, "resolved", name)],
        },
    )
    db.expire_all()
    db.refresh(opened[0])
    db.refresh(host)
    assert opened[0].status == "RESOLVED"
    assert db.query(IncidentNote).filter_by(incident_id=opened[0].id).count() >= 1
    assert (host.extras or {}).get(OUTAGES_KEY)
    db.close()


def test_invalid_payload_is_ignored():
    async def _run():
        async with get_db_session() as session:
            assert await process_incoming_alarm_state(session, {}) is None
            assert await process_incoming_alarm_state(session, {"labels": {}}) is None

    asyncio.run(_run())
