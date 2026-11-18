"""Pick one Core process to run the background loops.

Escalations, scheduled reports, syncs and the job queue must run exactly once, even
when Core is started with several web workers: two copies would send every escalation
mail twice. The process that holds a Postgres session-level advisory lock is the
leader; the others serve web requests only and keep trying, so a new leader takes over
within one loop interval when the old one dies (Postgres drops the lock with its
connection).

SQLite (unit tests, single process) has no advisory locks: every process is leader.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("forgesre")

# Any constant works; it only has to be the same in every Core process.
LEADER_LOCK_KEY = 0x464F524745  # "FORGE"


class LeaderLock:
    """Holds the advisory lock on a dedicated connection for as long as this process lives."""

    def __init__(self, connect: Callable[[], Any] | None, dialect: str) -> None:
        self._connect = connect
        self._dialect = dialect
        self._conn: Any = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._dialect != "postgresql" or self._conn is not None

    def ensure(self) -> bool:
        """True when this process is the leader. Tries to take the lock if it does not hold it.

        Cheap enough to call at the top of every loop iteration. Never raises: a database
        error means "not leader this round".
        """
        if self._dialect != "postgresql":
            return True
        with self._mutex:
            if self._conn is not None and self._alive():
                return True
            self._drop()
            try:
                conn = self._connect()
                cur = conn.cursor()
                cur.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_KEY,))
                got = bool(cur.fetchone()[0])
                cur.close()
                if not got:
                    conn.close()
                    return False
                self._conn = conn
                log.info("this Core process is now the background leader")
                return True
            except Exception as exc:  # database down or restarting
                log.warning("leader lock check failed: %s", exc)
                self._drop()
                return False

    def release(self) -> None:
        with self._mutex:
            self._drop()

    def _alive(self) -> bool:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            return True
        except Exception:
            return False

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()  # closing the session releases the advisory lock
            except Exception:
                pass
        self._conn = None


def _from_engine() -> LeaderLock:
    from app.db import engine

    dialect = engine.dialect.name

    def connect() -> Any:
        # A connection of its own, outside the pool: a pooled one could be handed to a
        # request while still holding the lock.
        cargs, cparams = engine.dialect.create_connect_args(engine.url)
        conn = engine.dialect.connect(*cargs, **cparams)
        conn.autocommit = True  # keep the lock session idle, not "idle in transaction"
        return conn

    return LeaderLock(connect if dialect == "postgresql" else None, dialect)


leader = _from_engine()
