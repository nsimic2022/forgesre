"""Async infrastructure scan: overlap lock, gather probes, own AsyncSession."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from app.asset_probe import AssetProbe, CheckResult
from app.db import get_db_session
from app.inventory import create_manual_asset
from app.jobs import (
    _infrastructure_scan_lock,
    infrastructure_scan_asset,
    run_periodic_infrastructure_check,
)
from app.seed import seed


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    n = uuid4().int
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0x7F or 1}"


def _probe(item, *, timeout=0.8, ok=True):
    del timeout
    host = str(item.get("ip") or "")
    icmp = CheckResult("icmp", ok, "reachable" if ok else "timeout", 1)
    metrics = CheckResult("metrics", None, "snmp", 0)
    return AssetProbe(
        asset_id=str(item.get("asset_id") or ""),
        hostname=str(item.get("hostname") or ""),
        ip=host,
        kind="network",
        type="Network device",
        scrape="",
        port=161,
        icmp=icmp,
        metrics=metrics,
    )


def test_infrastructure_scan_asset_picks_snmp_not_linux():
    db = _db()
    sw = create_manual_asset(
        db,
        hostname=f"scan-sw-{uuid4().hex[:6]}",
        ip=_ip(),
        type="Network device",
        actor="tester",
    )
    lnx = create_manual_asset(
        db,
        hostname=f"scan-lnx-{uuid4().hex[:6]}",
        ip=_ip(),
        type="Linux Server",
        actor="tester",
    )
    assert infrastructure_scan_asset(sw) is True
    assert infrastructure_scan_asset(lnx) is False
    db.close()


def test_scan_without_force_is_skipped_under_pytest():
    async def _run():
        return await run_periodic_infrastructure_check()

    result = asyncio.run(_run())
    assert result["skipped"] is True
    assert result["reason"] == "pytest"


def test_scan_probes_network_assets_in_parallel(monkeypatch):
    db = _db()
    first = create_manual_asset(
        db, hostname=f"scan-a-{uuid4().hex[:6]}", ip=_ip(), type="Network device", actor="tester"
    )
    second = create_manual_asset(
        db, hostname=f"scan-b-{uuid4().hex[:6]}", ip=_ip(), type="Network device", actor="tester"
    )
    seen: list[str] = []
    together = {"max": 0, "now": 0}

    def fake(item, timeout=0.8):
        together["now"] += 1
        together["max"] = max(together["max"], together["now"])
        time.sleep(0.05)
        seen.append(str(item.get("asset_id") or ""))
        together["now"] -= 1
        return _probe(item, timeout=timeout, ok=True)

    async def _run():
        return await run_periodic_infrastructure_check(force=True, probe_fn=fake)

    result = asyncio.run(_run())
    assert result["skipped"] is False
    assert result["scanned"] >= 2
    assert first.asset_id in seen and second.asset_id in seen
    assert together["max"] >= 2
    db.expire_all()
    db.refresh(first)
    db.refresh(second)
    assert first.ping_status == "green"
    assert second.ping_status == "green"
    db.close()


def test_overlap_lock_skips_second_cycle(monkeypatch):
    db = _db()
    create_manual_asset(
        db, hostname=f"scan-lock-{uuid4().hex[:6]}", ip=_ip(), type="Network device", actor="tester"
    )
    started = asyncio.Event()

    def slow(item, timeout=0.8):
        started.set()
        time.sleep(0.25)
        return _probe(item, timeout=timeout, ok=False)

    async def _run():
        first = asyncio.create_task(run_periodic_infrastructure_check(force=True, probe_fn=slow))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await run_periodic_infrastructure_check(force=True, probe_fn=slow)
        first_result = await first
        return first_result, second

    first_result, second = asyncio.run(_run())
    assert first_result["skipped"] is False
    assert second == {"skipped": True, "reason": "overlap", "scanned": 0, "updated": 0}
    assert not _infrastructure_scan_lock.locked()
    db.close()


def test_scan_uses_own_async_session():
    db = _db()
    host = create_manual_asset(
        db, hostname=f"scan-sess-{uuid4().hex[:6]}", ip=_ip(), type="Network device", actor="tester"
    )

    async def _run():
        async with get_db_session() as session:
            before = host.probe_checked_at
            result = await run_periodic_infrastructure_check(force=True, probe_fn=_probe)
            return result, before

    result, before = asyncio.run(_run())
    assert result["scanned"] >= 1
    db.expire_all()
    db.refresh(host)
    assert host.probe_checked_at is not None
    assert before != host.probe_checked_at
    db.close()
