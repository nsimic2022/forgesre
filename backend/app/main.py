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
from app.inventory import seed_demo_candidate, sync_netbox
from app.jobs import recover_interrupted_jobs, start_infrastructure_scheduler
from app.leader import leader
from app.loop_beats import beat
from app.webui import web_dist_dir
from app.metrics import metrics_response
from app.migrate import migrate
from app.models import Asset
from app.seed import DEMO_ASSET, seed, seed_summary
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


def process_role() -> str:
    """FORGESRE_ROLE: all (default: web and background loops), web (requests only) or worker (loops only)."""
    role = os.environ.get("FORGESRE_ROLE", "all").strip().lower()
    return role if role in {"all", "web", "worker"} else "all"


def runs_loops() -> bool:
    return process_role() in {"all", "worker"}


def start_loops(stop: threading.Event) -> None:
    """Escalation, jobs, ForgeAI jobs and discovery threads. Each runs its work only while this
    process holds the leader lock, so any number of processes can call this safely."""
    threading.Thread(target=_escalation_loop, args=(stop,), daemon=True, name="escalation").start()
    threading.Thread(target=_jobs_loop, args=(stop,), daemon=True, name="jobs").start()
    threading.Thread(target=_llm_jobs_loop, args=(stop,), daemon=True, name="llm-jobs").start()
    threading.Thread(target=_discovery_loop, args=(stop,), daemon=True, name="discovery").start()


def _discovery_loop(stop: threading.Event) -> None:
    import os

    from app.inventory import mark_discovery_loop_alive

    # Pytest sets FORGESRE_DISCOVERY_AUTO=0 — skip background syncs (a 30 s loop would
    # pollute the shared sqlite database).
    auto_raw = os.environ.get("FORGESRE_DISCOVERY_AUTO", "1").strip().lower()
    loop_disabled = auto_raw in {"0", "false", "off", "no"}

    first = True
    mark_discovery_loop_alive()
    beat("discovery", leader=leader.ensure())
    while not stop.is_set():
        delay = 30 if first else 6 * 60 * 60
        first = False
        if stop.wait(delay):
            break
        mark_discovery_loop_alive()
        if loop_disabled or not leader.ensure():
            continue
        beat("discovery")
        db = SessionLocal()
        try:
            # Discovery is LibreNMS's: queue a read of its devices (the jobs loop runs it).
            # CMDB HTTP below is AsyncClient + run_sync, not urllib on the FastAPI loop.
            if settings.discovery_enabled and settings.discovery_mode != "manual":
                from app.jobs import enqueue_discovery_scan
                from app.librenms_discovery import not_ready

                if not not_ready():
                    enqueue_discovery_scan(db)
                    db.commit()
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
        beat("jobs")
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
        beat("llm-jobs")
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
        beat("escalation")
        db = SessionLocal()
        try:
            from app.noise_filters import promote_filtered

            # Open quiet incidents whose noise filter stopped applying, then escalate.
            promote_filtered(db)
            process_escalations(db)
            from app.chat import sweep_resolved

            sweep_resolved(db)
            from app.tickets import sweep_resolved as tickets_resolved

            tickets_resolved(db)
            _queue_ticket_sync(db)
            _queue_oxidized_sync(db)
            _queue_health_checks(db)
            _queue_k8s_sync(db)
            _queue_snmp_apply(db)
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


_OXIDIZED_EVERY = 10 * 60
_oxidized_last = 0.0


def _queue_oxidized_sync(db: Session) -> None:
    """Leader only: every 10 minutes, read Oxidized for new config versions."""
    global _oxidized_last
    from app.oxidized import JOB_KIND, ready

    if ready() or time.monotonic() - _oxidized_last < _OXIDIZED_EVERY:
        return
    from app.jobs import enqueue

    _oxidized_last = time.monotonic()
    enqueue(db, JOB_KIND, "oxidized", object_type="changes")
    db.commit()


_TICKET_EVERY = 10 * 60
_ticket_last = 0.0


def _queue_ticket_sync(db: Session) -> None:
    """Leader only: every 10 minutes, read the status of open incidents' GLPI tickets."""
    global _ticket_last
    from app.tickets import SYNC_KIND, glpi_ready

    if glpi_ready() or time.monotonic() - _ticket_last < _TICKET_EVERY:
        return
    from app.jobs import enqueue

    _ticket_last = time.monotonic()
    enqueue(db, SYNC_KIND, "glpi", object_type="tickets")
    db.commit()


_SNMP_EVERY = 5 * 60
_snmp_last = 0.0


def _queue_snmp_apply(db: Session) -> None:
    """Leader only: every 5 minutes, write pending per-asset SNMP auths."""
    global _snmp_last
    if time.monotonic() - _snmp_last < _SNMP_EVERY:
        return
    from app.asset_snmp import auth_status
    from app.inventory import is_snmp_asset
    from app.jobs import enqueue
    from app.models import Asset
    from app.snmp_apply import JOB_KIND

    _snmp_last = time.monotonic()
    if any(is_snmp_asset(a) and auth_status(a).get("pending") for a in db.query(Asset).all()):
        enqueue(db, JOB_KIND, "snmp", object_type="snmp", payload={"actor": "system"})
        db.commit()


def _mount_web_app(app: FastAPI) -> None:
    """Serve the new React UI under /app when it has been built; every unknown /app/* path
    returns index.html so client-side routes survive a reload. Data comes from /api/v1."""
    import mimetypes

    mimetypes.add_type("application/manifest+json", ".webmanifest")  # PWA manifest (not in every mime table)
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
        log.info("ForgeSRE core started timezone=%s ai=%s role=%s", settings.timezone, settings.ai_enabled, process_role())
        app.state.escalation_stop = stop
        infra_stop = asyncio.Event()
        infra_task = None
        if runs_loops():
            start_loops(stop)
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
    _mount_web_app(app)
    app.mount("/mcp", asgi_app(mcp))  # read-only MCP; 404 until mcp.enabled and MCP_TOKEN are set
    app.include_router(api_router)
    app.include_router(web_router)
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
