from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.api import router as api_router
from app.db import Base, SessionLocal, engine
from app.journal import maybe_prune_old, prune_older_than, report
from app.inventory import run_scan, seed_demo_candidate, sync_netbox
from app.jobs import recover_interrupted_jobs, start_infrastructure_scheduler
from app.leader import leader
from app.webui import web_dist_dir
from app.metrics import metrics_response
from app.migrate import migrate
from app.models import Asset
from app.seed import DEMO_ASSET, seed, seed_summary
from app.settings import assert_runtime_secrets, settings
from app.bulk import router as bulk_router
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
    import os

    from app.inventory import mark_discovery_loop_alive

    # Pytest sets FORGESRE_DISCOVERY_AUTO=0 — skip background scans (tests mutate
    # discovery.cidrs in-process; a 30s loop would pollute shared sqlite).
    auto_raw = os.environ.get("FORGESRE_DISCOVERY_AUTO", "1").strip().lower()
    loop_disabled = auto_raw in {"0", "false", "off", "no"}

    first = True
    mark_discovery_loop_alive()
    while not stop.is_set():
        delay = 30 if first else 6 * 60 * 60
        first = False
        if stop.wait(delay):
            break
        mark_discovery_loop_alive()
        if loop_disabled or not leader.ensure():
            continue
        db = SessionLocal()
        try:
            # Scan is ICMP/TCP/SNMP in this thread (may hold the worker). CMDB
            # HTTP is AsyncClient + run_sync — asyncio.run here, not urllib on
            # the FastAPI loop.
            if settings.discovery_enabled and settings.discovery_mode != "manual":
                run_scan(db)
            if settings.netbox_enabled and settings.netbox_auto_sync:
                sync_netbox(db)
            if settings.zabbix_enabled and settings.zabbix_auto_sync:
                from app.zabbix_sync import sync_zabbix

                sync_zabbix(db)
            if settings.glpi_enabled and settings.glpi_auto_sync:
                from app.glpi_sync import sync_glpi

                sync_glpi(db)
            mark_discovery_loop_alive()
        except Exception as exc:
            log.exception("discovery loop failed")
            report(db, "discovery", "loop", "error", summary="Discovery loop failed", detail=str(exc))
        finally:
            db.close()


def _jobs_loop(stop: threading.Event) -> None:
    """Leader only: scheduled reports, non-LLM jobs, Zabbix poll, journal pruning, every 2 s."""
    from app.jobs import LANE_DEFAULT, run_pending_jobs
    from app.services import process_scheduled_reports
    from app.zabbix_sync import maybe_poll

    while not stop.wait(2):
        if not leader.ensure():
            continue
        db = SessionLocal()
        try:
            process_scheduled_reports(db)
            run_pending_jobs(db, lane=LANE_DEFAULT)
            maybe_poll(db)
            maybe_prune_old(db)
        except Exception as exc:
            log.exception("jobs loop failed")
            report(db, "rca", "jobs", "error", summary="Job worker failed", detail=str(exc))
        finally:
            db.close()


def _llm_jobs_loop(stop: threading.Event) -> None:
    """Leader only: ForgeAI rewrites in their own thread, one at a time, so a slow model
    never delays RCA, discovery or report mail."""
    from app.jobs import LANE_LLM, run_pending_jobs

    while not stop.wait(2):
        if not leader.ensure():
            continue
        db = SessionLocal()
        try:
            run_pending_jobs(db, limit=1, lane=LANE_LLM)
        except Exception as exc:
            log.exception("llm jobs loop failed")
            report(db, "rca", "llm-jobs", "error", summary="ForgeAI job worker failed", detail=str(exc))
        finally:
            db.close()


def _escalation_loop(stop: threading.Event) -> None:
    from app.services import process_escalations

    while not stop.wait(30):
        if not leader.ensure():
            continue
        db = SessionLocal()
        try:
            from app.noise_filters import promote_filtered

            # Open quiet incidents whose noise filter stopped applying, then escalate.
            promote_filtered(db)
            process_escalations(db)
            from app.chat import sweep_resolved

            sweep_resolved(db)
            _queue_health_checks(db)
            _queue_k8s_sync(db)
        except Exception as exc:
            log.exception("escalation loop failed")
            report(db, "escalation", "loop", "error", summary="Escalation loop failed", detail=str(exc))
        finally:
            db.close()


_HEALTH_EVERY = 15 * 60
_health_last = 0.0


def _queue_health_checks(db: Session) -> None:
    """Leader only (called from the escalation loop): queue TLS / backup / heartbeat checks every 15 min."""
    global _health_last
    if time.monotonic() - _health_last < _HEALTH_EVERY:
        return
    from app.jobs import enqueue

    _health_last = time.monotonic()
    enqueue(db, "health_checks", "appliance", object_type="checks")
    db.commit()


_K8S_EVERY = 5 * 60
_k8s_last = 0.0


def _queue_k8s_sync(db: Session) -> None:
    """Leader only: read configured Kubernetes / OpenShift clusters every 5 minutes."""
    global _k8s_last
    from app.kubernetes import clusters

    if not clusters() or time.monotonic() - _k8s_last < _K8S_EVERY:
        return
    from app.jobs import enqueue

    _k8s_last = time.monotonic()
    enqueue(db, "k8s_sync", "clusters", object_type="kubernetes")
    db.commit()


def _mount_web_app(app: FastAPI) -> None:
    """Serve the new React UI under /app when it has been built; every unknown /app/* path
    returns index.html so client-side routes survive a reload. Data comes from /api/v1."""
    dist = web_dist_dir()
    index = dist / "index.html"
    if not index.is_file():
        log.info("new UI not built (%s missing); /app is off", index)
        return
    if (dist / "assets").is_dir():
        app.mount("/app/assets", StaticFiles(directory=str(dist / "assets")), name="web-assets")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{rest:path}", include_in_schema=False)
    def web_app(rest: str = "") -> Response:
        target = (dist / rest).resolve()
        if rest and target.is_file() and dist.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(index, headers={"Cache-Control": "no-store"})


def create_app() -> FastAPI:
    assert_runtime_secrets()
    configure_logging()
    stop = threading.Event()
    from app.mcp_server import asgi_app, build_server

    mcp = build_server()  # one per app: its session manager runs inside this app's lifespan

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop.clear()
        Base.metadata.create_all(bind=engine)
        migrate(engine)
        db: Session = SessionLocal()
        try:
            seed(db)
            seed_demo_candidate(db)
            # Only the leader may reset "running" jobs: in another process they may be live.
            if leader.ensure():
                recover_interrupted_jobs(db)
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
                summary=seed_summary(db),
                object_type="asset",
                object_id=DEMO_ASSET if db.query(Asset.id).filter_by(asset_id=DEMO_ASSET).first() else "",
            )
            prune_older_than(db)
        except Exception as exc:
            log.exception("startup failed")
            report(db, "core", "startup", "error", summary="Core startup failed", detail=str(exc))
            raise
        finally:
            db.close()
        log.info("ForgeSRE core started timezone=%s ai=%s", settings.timezone, settings.ai_enabled)
        threading.Thread(target=_escalation_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=_jobs_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=_llm_jobs_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=_discovery_loop, args=(stop,), daemon=True).start()
        app.state.escalation_stop = stop
        infra_stop = asyncio.Event()
        infra_task = None
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            infra_task = asyncio.create_task(start_infrastructure_scheduler(stop=infra_stop))
        app.state.infra_scan_stop = infra_stop
        async with mcp.session_manager.run():
            yield
        infra_stop.set()
        if infra_task is not None:
            infra_task.cancel()
        stop.set()
        leader.release()

    app = FastAPI(title="ForgeSRE", version="2.0.0", lifespan=lifespan)
    static_dir = settings.frontend_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    _mount_web_app(app)
    app.mount("/mcp", asgi_app(mcp))  # read-only MCP; 404 until mcp.enabled and MCP_TOKEN are set
    app.include_router(api_router)
    app.include_router(web_router)
    app.include_router(bulk_router)
    from app.api import librenms_webhook, zabbix_webhook

    # Short aliases for media types typed by hand (same handlers as /api/v1/webhooks/...).
    app.add_api_route("/webhooks/zabbix", zabbix_webhook, methods=["POST"], include_in_schema=False)
    app.add_api_route("/webhooks/librenms", librenms_webhook, methods=["POST"], include_in_schema=False)

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
