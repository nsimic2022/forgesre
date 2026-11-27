"""LibreNMS ↔ ForgeSRE, block by block (registered in app.flows).

Out: settings → DNS/TCP(/TLS) → web UI up → API token accepted (device list) → last topology
sync. In: LibreNMS alert transport → network → ForgeSRE webhook → incident. Read-only, with the
LibreNMS client's own timeout and backoff; the token is never shown.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.flows import Flow, http_step, journal_step, network_steps, received_steps, register, step
from app.settings import settings


def _out_lane(db: Session) -> list[dict[str, Any]]:
    from app import librenms

    url = settings.librenms_url
    steps = [step("cfg", "Settings", "URL and token", "ok" if settings.librenms_token else "bad",
                  f"{url}; API token {'set' if settings.librenms_token else 'empty'}.",
                  fix="" if settings.librenms_token else "LibreNMS › Settings › API › Create token, then ./forgesre settings set-secret LIBRENMS_API_TOKEN")]
    net, ok = network_steps(url, what="LibreNMS")
    steps += net
    if not ok:
        return steps
    ui, _ = http_step("ui", "Web UI", url.rstrip("/") + "/login", ok_text="The LibreNMS web UI answers.",
                      fix="docker compose logs librenms (first boot migrates MariaDB for a few minutes)")
    steps.append(ui)
    if settings.librenms_token and ui["status"] == "ok":
        started = time.monotonic()
        try:
            devices = librenms.run_sync(librenms.list_devices())
            steps.append(step("api", "API", "GET /devices", "ok", f"Token accepted; {len(devices)} device(s) in LibreNMS.",
                              ms=int((time.monotonic() - started) * 1000), log={"direction": "out", "kinds": ["GET /devices"]}))
        except librenms.LibreNmsError as exc:
            steps.append(step("api", "API", "GET /devices", "bad", f"LibreNMS refused: {exc}.", ms=int((time.monotonic() - started) * 1000),
                              fix="Check LIBRENMS_API_TOKEN (LibreNMS › Settings › API) and that its user may read devices.",
                              log={"direction": "out", "kinds": ["GET /devices"]}))
    steps.append(journal_step(db, "sync", "Last sync", "inventory", "topology.sync", never="LibreNMS was never read.",
                              fix="Discovery › Sync hosts › LibreNMS"))
    return steps


def check(db: Session) -> dict[str, Any]:
    """Both directions: ForgeSRE reading LibreNMS, and LibreNMS alerts posted to ForgeSRE."""
    inn = received_steps(db, "librenms", sender="LibreNMS", endpoint="/api/v1/webhooks/librenms", token_name="ALERTMANAGER_WEBHOOK_TOKEN",
                         token_set=bool(settings.webhook_token), incident_source="librenms")
    return {"lanes": [{"id": "in", "title": "LibreNMS → ForgeSRE (alert transport)", "steps": inn},
                      {"id": "out", "title": "ForgeSRE → LibreNMS (API, read-only)", "steps": _out_lane(db)}]}


register(Flow(
    name="librenms", label="LibreNMS", check=check, configured=lambda: bool(settings.librenms_enabled),
    log_words=("librenms",), services=("core", "librenms"),
    about="LibreNMS alerts posted to ForgeSRE, and ForgeSRE reading LibreNMS devices and links.",
))
