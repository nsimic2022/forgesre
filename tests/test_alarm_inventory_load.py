"""Alarm/inventory reliability: selectinload on async, threads without sync HTTP."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect as sa_inspect, select

from app.asset_alarms import _INCIDENT_ASYNC, _resolve_active_incident, process_incoming_alarm_states
from app.db import get_db_session
from app.inventory import create_manual_asset
from app.models import Evidence, Incident
from app.seed import seed
from app.services import alert_fingerprint

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    n = uuid4().int
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0x7F or 1}"


def _open_incident(db, host, *, fingerprint: str | None = None) -> Incident:
    started = datetime.now(timezone.utc) - timedelta(minutes=8)
    row = Incident(
        number=f"INC-LOAD-{uuid4().hex[:6]}",
        title="box down",
        severity="CRITICAL",
        status="OPEN",
        fingerprint=fingerprint or f"SnmpDeviceUnreachable:{host.asset_id}",
        source="snmp",
        asset_id=host.id,
        started_at=started,
        timeline=[],
        alert_payload={},
    )
    db.add(row)
    db.flush()
    db.add(
        Evidence(
            incident_id=row.id,
            kind="metrics",
            title="snmp",
            payload={"ok": True},
        )
    )
    host.status = "critical"
    db.commit()
    db.refresh(row)
    return row


def test_async_incident_queries_selectinload_asset_and_evidence():
    src = (APP / "asset_alarms.py").read_text(encoding="utf-8")
    assert "selectinload(Incident.asset)" in src
    assert "selectinload(Incident.evidence)" in src
    assert src.count("select(Incident)") == 3
    assert src.count(".options(*_INCIDENT_ASYNC)") == 3
    services = (APP / "services.py").read_text(encoding="utf-8")
    assert "joinedload(Incident.asset)" in services
    assert "selectinload(Incident.evidence)" in services
    jobs = (APP / "jobs.py").read_text(encoding="utf-8")
    assert "joinedload(Incident.asset)" in jobs
    assert "selectinload(Incident.evidence)" in jobs


def test_loops_stay_threads_without_sync_http_on_fastapi():
    main = (APP / "main.py").read_text(encoding="utf-8")
    assert "threading.Thread(target=_escalation_loop" in main
    assert "threading.Thread(target=_jobs_loop" in main
    assert "threading.Thread(target=_discovery_loop" in main
    assert "asyncio.create_task(start_infrastructure_scheduler" in main
    assert "import urllib" not in main
    assert "urllib.request" not in main
    assert "httpx.Client(" not in main
    jobs = (APP / "jobs.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread" in jobs
    assert "_infrastructure_scan_lock" in jobs
    models = (APP / "models.py").read_text(encoding="utf-8")
    assert "asset: Mapped[Asset | None] = relationship(back_populates=\"incidents\")" in models
    assert "evidence: Mapped[list[Evidence]] = relationship(back_populates=\"incident\"" in models
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "selectinload" in arch
    assert "no lazy-load from `AsyncSession`" in arch


def test_resolve_active_incident_eager_loads_asset_and_evidence():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"load-up-{uuid4().hex[:6]}", ip=ip, type="Network device", actor="tester"
    )
    fingerprint = f"SnmpDeviceUnreachable:{host.asset_id}"
    _open_incident(db, host, fingerprint=fingerprint)

    async def _run():
        async with get_db_session() as session:
            closed = await _resolve_active_incident(
                session,
                await session.get(type(host), host.id),
                fingerprint,
                alertname="SnmpDeviceUnreachable",
                resolver="LibreNMS",
                labels={"alertname": "SnmpDeviceUnreachable"},
            )
            assert closed is not None
            state = sa_inspect(closed)
            assert "asset" not in state.unloaded
            assert "evidence" not in state.unloaded
            assert closed.asset is not None
            assert closed.asset.hostname == host.hostname
            titles = [item.title for item in closed.evidence]
            assert "snmp" in titles
            return closed.number

    number = asyncio.run(_run())
    assert number
    db.close()


def test_batch_alarm_states_eager_load_relationships():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"load-batch-{uuid4().hex[:6]}", ip=ip, type="Network device", actor="tester"
    )
    alert = {
        "status": "resolved",
        "labels": {
            "alertname": "SnmpDeviceUnreachable",
            "asset": host.asset_id,
            "instance": f"{ip}:161",
            "ip": ip,
            "severity": "critical",
        },
        "annotations": {"summary": "SnmpDeviceUnreachable"},
    }
    _open_incident(db, host, fingerprint=alert_fingerprint(alert))

    async def _run():
        async with get_db_session() as session:
            results = await process_incoming_alarm_states(session, [alert])
            row = await session.scalar(
                select(Incident)
                .options(*_INCIDENT_ASYNC)
                .where(Incident.fingerprint == alert_fingerprint(alert))
            )
            assert row is not None
            state = sa_inspect(row)
            assert "asset" not in state.unloaded
            assert "evidence" not in state.unloaded
            hostname = row.asset.hostname if row.asset else None
            titles = [item.title for item in row.evidence]
            assert "snmp" in titles
            return results, hostname

    results, hostname = asyncio.run(_run())
    assert results[0]["status"] == "resolved"
    assert hostname == host.hostname
    db.close()
