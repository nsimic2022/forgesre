"""ForgeSRE worker process: the background loops without the web server.

``docker compose --profile worker up -d worker`` (or ``python -m app.worker``) runs escalations,
scheduled reports, the job queue, ForgeAI jobs, discovery and syncs here, so the web workers
only answer requests. Start Core with ``FORGESRE_ROLE=web`` to leave the loops to this process;
with the default ``FORGESRE_ROLE=all`` both may run them, and the Postgres leader lock still lets
only one do the work at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading

log = logging.getLogger("forgesre")


def main() -> None:
    """Create tables, take part in the leader election and run the loops until SIGTERM."""
    os.environ.setdefault("FORGESRE_ROLE", "worker")
    from app.db import Base, SessionLocal, engine
    from app.jobs import recover_interrupted_jobs, start_infrastructure_scheduler
    from app.journal import report
    from app.leader import leader
    from app.main import configure_logging, start_loops
    from app.migrate import migrate
    from app.settings import assert_runtime_secrets

    assert_runtime_secrets()
    configure_logging()
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    try:
        if leader.ensure():
            recover_interrupted_jobs(db)
        report(db, "core", "worker", "ok", summary=f"Worker started pid={os.getpid()} leader={leader.held}")
    finally:
        db.close()
    stop = threading.Event()
    start_loops(stop)

    async def run() -> None:
        infra_stop = asyncio.Event()
        task = asyncio.create_task(start_infrastructure_scheduler(stop=infra_stop))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, infra_stop.set)
        await infra_stop.wait()
        task.cancel()

    log.info("ForgeSRE worker started")
    try:
        asyncio.run(run())
    finally:
        stop.set()
        leader.release()
        log.info("ForgeSRE worker stopped")


if __name__ == "__main__":
    main()
