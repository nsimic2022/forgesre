"""Database engines and sessions.

FastAPI routes and CLI keep the sync ``SessionLocal`` / ``get_db()`` path
(psycopg2 or sqlite). Background webhook ingest uses ``get_db_session()`` —
an async SQLAlchemy session on ``postgresql+asyncpg`` (aiosqlite under pytest)
with a DC-sized pool so a burst of alarms does not hit QueuePool limits.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.settings import settings

log = logging.getLogger("forgesre.db")

POOL_SIZE = 20
MAX_OVERFLOW = 10
POOL_TIMEOUT = 30
POOL_RECYCLE = 1800


class Base(DeclarativeBase):
    pass


def _is_postgres(url: str) -> bool:
    return (url or "").startswith("postgresql")


def _is_sqlite(url: str) -> bool:
    return (url or "").startswith("sqlite")


def sync_database_url(url: str) -> str:
    """psycopg2 for the sync engine. ``postgresql+asyncpg://`` is rewritten."""
    raw = (url or "").strip()
    if raw.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg2://" + raw.split("://", 1)[1]
    return raw


def async_database_url(url: str) -> str:
    """asyncpg on Postgres; aiosqlite when tests set ``sqlite:///``."""
    raw = (url or "").strip()
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + raw.split("://", 1)[1]
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.split("://", 1)[1]
    if raw.startswith("sqlite+aiosqlite://"):
        return raw
    if raw.startswith("sqlite://"):
        return raw.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return raw


def _postgres_pool_kwargs() -> dict[str, int]:
    return {
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_timeout": POOL_TIMEOUT,
        "pool_recycle": POOL_RECYCLE,
    }


def _sync_engine_kwargs(url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if _is_postgres(url):
        kwargs.update(_postgres_pool_kwargs())
    return kwargs


def _async_engine_kwargs(url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"echo": False}
    if _is_postgres(url):
        kwargs.update(_postgres_pool_kwargs())
        kwargs["pool_pre_ping"] = True
    return kwargs


SYNC_DATABASE_URL = sync_database_url(settings.database_url)
ASYNC_DATABASE_URL = async_database_url(settings.database_url)

engine = create_engine(SYNC_DATABASE_URL, **_sync_engine_kwargs(SYNC_DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

async_engine = create_async_engine(ASYNC_DATABASE_URL, **_async_engine_kwargs(ASYNC_DATABASE_URL))
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Async session: commit on success, rollback on error, always close (back to the pool)."""
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        log.exception("db session rolled back")
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Connectivity check for the async engine. Schema stays with migrate()."""
    async with async_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
