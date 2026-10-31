"""Async inventory: IP check on AsyncSession, Auto detect, fire-and-forget render."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from app.db import get_db_session
from app.exporter_detect import AUTO_ASSET_TYPE, ExporterDetect, detect_exporter_type
from app.inventory import (
    add_asset,
    create_manual_asset,
    find_asset_by_ip,
    trigger_monitoring_render,
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


def test_detect_exporter_type_gathers_fetcher_in_parallel():
    seen: list[str] = []

    def fake_fetch(url, timeout):
        del timeout
        seen.append(url)
        time.sleep(0.25)
        if ":9100" in url:
            return 200, "node_cpu_seconds_total 1\n", ""
        return None, "", "timeout"

    async def _run():
        started = time.monotonic()
        result = await detect_exporter_type("10.1.2.3", fetcher=fake_fetch, snmp_ok=False)
        elapsed = time.monotonic() - started
        assert result.kind == "linux"
        assert result.asset_type == "Linux Server"
        assert elapsed < 0.45

    asyncio.run(_run())
    assert any(":9100" in url for url in seen)
    assert any(":9182" in url for url in seen)


def test_find_asset_by_ip_uses_async_session():
    db = _db()
    ip = _ip()
    host = create_manual_asset(db, hostname=f"async-ip-{uuid4().hex[:6]}", ip=ip, actor="tester")

    async def _run():
        async with get_db_session() as session:
            found = await find_asset_by_ip(session, ip)
            missing = await find_asset_by_ip(session, "10.255.255.254")
            empty = await find_asset_by_ip(session, "")
        assert found is not None
        assert found.asset_id == host.asset_id
        assert missing is None
        assert empty is None

    asyncio.run(_run())
    db.close()


def test_add_asset_auto_awaits_detect_exporter_type(monkeypatch):
    db = _db()
    ip = _ip()
    seen: list[str] = []

    async def fake_detect(ip_addr, hint_type="", **kwargs):
        del hint_type, kwargs
        seen.append(ip_addr)
        return ExporterDetect(
            kind="windows",
            asset_type="Windows Server",
            scrape_address=f"{ip_addr}:9182",
            profile="windows-standard",
            port=9182,
            windows=True,
            message=f"Detected windows_exporter on {ip_addr}",
        )

    monkeypatch.setattr("app.inventory.detect_exporter_type", fake_detect)

    async def _run():
        async with get_db_session() as session:
            return await add_asset(
                session,
                write_db=db,
                hostname=f"async-auto-{uuid4().hex[:6]}",
                ip=ip,
                type=AUTO_ASSET_TYPE,
                actor="tester",
            )

    asset = asyncio.run(_run())
    assert seen == [ip]
    assert asset.type == "Windows Server"
    assert asset.scrape_address == f"{ip}:9182"
    assert getattr(asset, "_detect_message", "").startswith("Detected windows_exporter")
    db.close()


def test_add_asset_rejects_duplicate_ip():
    db = _db()
    ip = _ip()
    first = create_manual_asset(db, hostname=f"async-dup-{uuid4().hex[:6]}", ip=ip, actor="tester")

    async def _run():
        async with get_db_session() as session:
            with pytest.raises(ValueError, match="already uses"):
                await add_asset(
                    session,
                    write_db=db,
                    hostname=f"async-dup2-{uuid4().hex[:6]}",
                    ip=ip,
                    actor="tester",
                    require_new=True,
                )
            reused = await add_asset(
                session,
                write_db=db,
                hostname=f"async-dup3-{uuid4().hex[:6]}",
                ip=ip,
                actor="tester",
            )
            assert reused.asset_id == first.asset_id

    asyncio.run(_run())
    db.close()


def test_trigger_monitoring_render_is_fire_and_forget(monkeypatch):
    seen: dict = {}

    class FakeProc:
        pid = 4242

        async def wait(self):
            seen["waited"] = True

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr("app.inventory._skip_monitoring_render", lambda: False)
    monkeypatch.setattr("app.inventory.asyncio.create_subprocess_exec", fake_exec)

    async def _run():
        proc = await trigger_monitoring_render()
        assert proc is not None and proc.pid == 4242
        assert seen["args"][0].endswith("scripts/render-monitoring.sh")
        assert "waited" not in seen
        await asyncio.sleep(0)
        assert seen["waited"] is True

    asyncio.run(_run())


def test_trigger_monitoring_render_skipped_under_pytest(monkeypatch):
    called = []

    async def fake_exec(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("must not spawn under pytest")

    monkeypatch.setattr("app.inventory.asyncio.create_subprocess_exec", fake_exec)

    async def _run():
        assert await trigger_monitoring_render() is None

    asyncio.run(_run())
    assert called == []


def test_add_asset_network_queues_render(monkeypatch):
    db = _db()
    ip = _ip()
    queued: list[str] = []

    async def fake_render():
        queued.append("render")
        return None

    monkeypatch.setattr("app.inventory.trigger_monitoring_render", fake_render)

    async def _run():
        async with get_db_session() as session:
            return await add_asset(
                session,
                write_db=db,
                hostname=f"async-sw-{uuid4().hex[:6]}",
                ip=ip,
                type="Network device",
                actor="tester",
            )

    asset = asyncio.run(_run())
    assert asset.type == "Network device"
    assert queued == ["render"]
    db.close()
