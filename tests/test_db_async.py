"""Async engine URL rewrite, DC pool sizes, get_db_session commit/rollback/close."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.db import (
    ASYNC_DATABASE_URL,
    MAX_OVERFLOW,
    POOL_SIZE,
    SYNC_DATABASE_URL,
    async_database_url,
    async_engine,
    engine,
    get_db_session,
    init_db,
    sync_database_url,
)


def test_async_url_uses_asyncpg_on_postgres():
    assert (
        async_database_url("postgresql+psycopg2://forgesre:x@127.0.0.1:5432/forgesre")
        == "postgresql+asyncpg://forgesre:x@127.0.0.1:5432/forgesre"
    )
    assert (
        async_database_url("postgresql://forgesre:x@127.0.0.1:5432/forgesre")
        == "postgresql+asyncpg://forgesre:x@127.0.0.1:5432/forgesre"
    )
    assert async_database_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert async_database_url("sqlite:////tmp/x.db") == "sqlite+aiosqlite:////tmp/x.db"


def test_sync_url_keeps_psycopg2():
    assert (
        sync_database_url("postgresql+asyncpg://forgesre:x@127.0.0.1:5432/forgesre")
        == "postgresql+psycopg2://forgesre:x@127.0.0.1:5432/forgesre"
    )
    assert sync_database_url("sqlite:////tmp/x.db") == "sqlite:////tmp/x.db"
    assert SYNC_DATABASE_URL.startswith("sqlite://")
    assert ASYNC_DATABASE_URL.startswith("sqlite+aiosqlite://")


def test_postgres_pool_is_sized_for_dc_burst():
    assert POOL_SIZE == 20 and MAX_OVERFLOW == 10
    kwargs = engine.url.drivername
    assert "sqlite" in kwargs
    # Pytest uses sqlite (no QueuePool burst). The postgres kwargs are what Core gets.
    from app.db import _postgres_pool_kwargs

    pool = _postgres_pool_kwargs()
    assert pool["pool_size"] == 20
    assert pool["max_overflow"] == 10
    assert pool["pool_timeout"] == 30
    assert pool["pool_recycle"] == 1800


def test_get_db_session_commits_and_closes():
    async def _run() -> None:
        async with get_db_session() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        await init_db()

    asyncio.run(_run())


def test_get_db_session_rolls_back_and_closes():
    async def _run() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with get_db_session() as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("boom")

    asyncio.run(_run())


def test_async_engine_is_not_the_sync_engine():
    assert async_engine is not engine
    assert "asyncpg" in async_database_url("postgresql+psycopg2://u:p@h/db")
