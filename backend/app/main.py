from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.api import router as api_router
from app.db import Base, SessionLocal, engine
from app.metrics import metrics_response
from app.seed import seed
from app.settings import settings
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


def _escalation_loop(stop: threading.Event) -> None:
    from app.services import process_escalations

    while not stop.wait(30):
        db = SessionLocal()
        try:
            process_escalations(db)
        except Exception:
            log.exception("escalation loop failed")
        finally:
            db.close()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="ForgeSRE", version="0.1.0")
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

    stop = threading.Event()

    @app.on_event("startup")
    def on_startup() -> None:
        Base.metadata.create_all(bind=engine)
        db: Session = SessionLocal()
        try:
            seed(db)
        finally:
            db.close()
        log.info("ForgeSRE core started timezone=%s ai=%s", settings.timezone, settings.ai_enabled)
        thread = threading.Thread(target=_escalation_loop, args=(stop,), daemon=True)
        thread.start()
        app.state.escalation_stop = stop

    @app.on_event("shutdown")
    def on_shutdown() -> None:
        stop.set()

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
