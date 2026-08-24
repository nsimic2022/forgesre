from __future__ import annotations

import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.api import router as api_router
from app.db import Base, SessionLocal, engine
from app.journal import report
from app.inventory import run_scan, seed_demo_candidate, sync_netbox
from app.metrics import metrics_response
from app.migrate import migrate
from app.seed import seed
from app.settings import assert_runtime_secrets, settings
from app.web import NotAuthenticated, router as web_router

log = logging.getLogger("forgesre")


def configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        path = Path(settings.log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(path))
        except OSError:
            log.warning("cannot write log file %s", settings.log_file)
    logging.basicConfig(
        level=getattr(logging, str(settings.yaml.get("system", {}).get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _discovery_loop(stop: threading.Event) -> None:
    first = True
    while not stop.is_set():
        delay = 30 if first else 6 * 60 * 60
        first = False
        if stop.wait(delay):
            break
        db = SessionLocal()
        try:
            if settings.discovery_enabled and settings.discovery_mode != "manual":
                run_scan(db)
            if settings.netbox_enabled:
                sync_netbox(db)
        except Exception as exc:
            log.exception("discovery loop failed")
            report(db, "discovery", "loop", "error", summary="Discovery loop failed", detail=str(exc))
        finally:
            db.close()


def _jobs_loop(stop: threading.Event) -> None:
    from app.jobs import run_pending_jobs

    while not stop.wait(2):
        db = SessionLocal()
        try:
            run_pending_jobs(db)
            from app.services import process_scheduled_reports

            process_scheduled_reports(db)
        except Exception as exc:
            log.exception("jobs loop failed")
            report(db, "rca", "jobs", "error", summary="Job worker failed", detail=str(exc))
        finally:
            db.close()


def _escalation_loop(stop: threading.Event) -> None:
    from app.services import process_escalations

    while not stop.wait(30):
        db = SessionLocal()
        try:
            process_escalations(db)
        except Exception as exc:
            log.exception("escalation loop failed")
            report(db, "escalation", "loop", "error", summary="Escalation loop failed", detail=str(exc))
        finally:
            db.close()


def create_app() -> FastAPI:
    assert_runtime_secrets()
    configure_logging()
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop.clear()
        Base.metadata.create_all(bind=engine)
        migrate(engine)
        db: Session = SessionLocal()
        try:
            seed(db)
            seed_demo_candidate(db)
            report(
                db,
                "core",
                "startup",
                "ok",
                summary=f"Core started timezone={settings.timezone} ai={settings.ai_enabled}",
            )
            report(
                db,
                "seed",
                "seed",
                "ok",
                summary="Demo asset, playrules, and closed HighCPU history are ready",
                object_type="asset",
                object_id="forge-demo-01",
            )
        except Exception as exc:
            log.exception("startup failed")
            report(db, "core", "startup", "error", summary="Core startup failed", detail=str(exc))
            raise
        finally:
            db.close()
        log.info("ForgeSRE core started timezone=%s ai=%s", settings.timezone, settings.ai_enabled)
        threading.Thread(target=_escalation_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=_jobs_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=_discovery_loop, args=(stop,), daemon=True).start()
        app.state.escalation_stop = stop
        yield
        stop.set()

    app = FastAPI(title="ForgeSRE", version="0.7.0", lifespan=lifespan)
    static_dir = settings.frontend_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(api_router)
    app.include_router(web_router)

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request, exc):  # noqa: ARG001
        return RedirectResponse("/login", status_code=302)

    @app.get("/metrics")
    def metrics() -> Response:
        body, content_type = metrics_response()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
