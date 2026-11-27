"""ForgeSRE → NetBox: the read-only inventory path, block by block (registered in app.flows).

Settings → DNS/TCP/TLS → web UI → API token (one device read) → last sync → linked assets.
Uses app.netbox's status probe (5 s); the token is never shown.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.flows import Flow, journal_step, network_steps, register, step
from app.settings import settings


def check(db: Session) -> dict[str, Any]:
    """Settings, network, the API read and the last sync."""
    from app.models import Asset
    from app.netbox import netbox_status

    url, token = settings.netbox_url, settings.netbox_token
    steps = [step("cfg", "Settings", "URL and token", "ok" if url and token else "bad",
                  f"{url or 'no URL'}; API token {'set' if token else 'empty'}.",
                  fix="" if url and token else "./forgesre settings set-secret NETBOX_API_TOKEN (NetBox › Admin › API tokens, read-only)")]
    if url:
        net, ok = network_steps(url, what="NetBox")
        steps += net
        if ok:
            started = time.monotonic()
            status = netbox_status(url, token)
            good = bool(status.get("ok")) or status.get("light") == "green"
            steps.append(step("api", "API", "GET /api/dcim/devices", "ok" if good else ("warn" if status.get("starting") else "bad"),
                              str(status.get("why") or (f"{status.get('count', 0)} device(s) readable." if good else "No answer.")),
                              ms=int((time.monotonic() - started) * 1000),
                              fix="" if good else "A 403 is the token's permissions (read on DCIM devices); 'not answering yet' is a first boot."))
    steps.append(journal_step(db, "sync", "Last sync", "netbox", "sync", never="NetBox was never synced.", fix="Discovery › Sync hosts › NetBox"))
    linked = db.query(Asset).filter(Asset.netbox_id != "").count()
    steps.append(step("assets", "Inventory", f"{linked} linked", "ok" if linked else "unknown",
                      f"{linked} asset(s) in ForgeSRE come from NetBox." if linked else "No asset is linked to NetBox yet."))
    return {"lanes": [{"id": "out", "title": "ForgeSRE → NetBox (read-only)", "steps": steps}]}


register(Flow(
    name="netbox", label="NetBox", check=check, configured=lambda: bool(settings.netbox_enabled),
    log_words=("netbox",), services=("core", "netbox"),
    about="How ForgeSRE reads devices, sites and racks from NetBox. ForgeSRE never writes to it.",
))
