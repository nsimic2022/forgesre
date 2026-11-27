"""ForgeSRE → GLPI: the read-only inventory path, block by block (registered in app.flows).

Settings (URL and tokens set; values never shown) → DNS → TCP → TLS → initSession (tokens
accepted) → the API user's profile and its read rights → Computer and NetworkEquipment readable →
killSession. Then inside ForgeSRE: backoff state, the last sync and the assets linked to GLPI. Last
lane, when tickets.glpi is on: the right to create tickets and the last ticket calls (app.tickets).
The check itself only GETs, with settings.glpi_timeout; it never creates a ticket.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.flows import Flow, network_steps, register, step
from app.settings import settings

READ, CREATE = 1, 4  # GLPI right bits
LOGIN_FIX = {
    "ERROR_GLPI_LOGIN_USER_TOKEN": "The user token is wrong: GLPI › the API user › Remote access key, then ./forgesre settings set-secret GLPI_USER_TOKEN.",
    "ERROR_WRONG_APP_TOKEN_PARAMETER": "The App-Token is wrong: GLPI › Setup › General › API › the API client, then ./forgesre settings set-secret GLPI_APP_TOKEN.",
    "ERROR_APP_TOKEN_PARAMETERS_MISSING": "GLPI wants an App-Token: ./forgesre settings set-secret GLPI_APP_TOKEN.",
    "ERROR_NOT_ALLOWED_IP": "GLPI refuses this server's IP: add it to the API client's IP range in GLPI › Setup › General › API.",
    "ERROR_API_DISABLED": "The REST API is off: GLPI › Setup › General › API › Enable Rest API.",
}


def _fix_for(why: str) -> str:
    return next((fix for code, fix in LOGIN_FIX.items() if code in why), "Open the GLPI API log (Setup › General › API) for the refused call.")


def _call(fn: Any) -> tuple[Any, int, str]:
    from app.glpi import GlpiError

    started = time.monotonic()
    try:
        value = fn()
        return value, int((time.monotonic() - started) * 1000), ""
    except GlpiError as exc:
        return None, int((time.monotonic() - started) * 1000), str(exc)


def _api_steps(profile_out: dict[str, Any]) -> list[dict[str, Any]]:
    """Session, profile and asset blocks; the active profile is copied into ``profile_out``."""
    from app import glpi

    url, app, user = settings.glpi_url, settings.glpi_app_token, settings.glpi_user_token
    timeout = settings.glpi_timeout
    steps: list[dict[str, Any]] = []
    session, ms, err = _call(lambda: glpi.init_session(url, app, user, timeout=timeout, force=True))
    if err:
        steps.append(step("session", "Sign-in", "initSession", "bad", f"GLPI refused the session: {err}.", ms=ms, fix=_fix_for(err),
                          log={"direction": "out", "kinds": ["initSession"]}))
        return steps
    steps.append(step("session", "Sign-in", "initSession", "ok", "GLPI accepted the App-Token and user token.", ms=ms,
                      log={"direction": "out", "kinds": ["initSession"]}))
    try:
        full, ms, err = _call(lambda: glpi._get("getFullSession", url=url, app_token=app, session=session, timeout=timeout, force=True))
        profile = ((full or {}).get("session") or {}).get("glpiactiveprofile") or {} if isinstance(full, dict) else {}
        profile_out.update(profile if isinstance(profile, dict) else {})
        if err:
            steps.append(step("profile", "Profile", "getFullSession", "warn", f"Could not read the session: {err}.", ms=ms))
        else:
            rights = {key: profile.get(key) for key in ("computer", "networking")}
            missing = [label for key, label in (("computer", "Computer"), ("networking", "NetworkEquipment"))
                       if isinstance(rights[key], int) and not rights[key] & READ]
            name = str(profile.get("name") or "?")
            steps.append(step("profile", "Profile", name, "bad" if missing else "ok",
                              f"Profile {name} cannot read {', '.join(missing)}." if missing else f"Profile {name} can read Computer and NetworkEquipment.",
                              ms=ms, fix="Give the API user's profile Read on Assets › Computers and Network devices." if missing else ""))
        rows = []
        for itemtype in glpi.ITEMTYPES:
            got, ms, err = _call(lambda t=itemtype: glpi._as_rows(glpi._get(t, url=url, app_token=app, session=session, timeout=timeout,
                                                                            params={"range": "0-0"}, force=True)))
            rows.append({"at": "", "status": "error" if err else ("readable" if got else "none"), "error": err[:160], "subject": itemtype, "ms": ms})
        errors = [r for r in rows if r["status"] == "error"]
        empty = [r for r in rows if r["status"] == "none"]
        steps.append(step("items", "Assets", "Computer, NetworkEquipment", "bad" if errors else ("warn" if len(empty) == len(rows) else "ok"),
                          ("GLPI refused: " + "; ".join(f"{r['subject']}: {r['error']}" for r in errors)) if errors else
                          ("The API user sees no Computer or NetworkEquipment." if len(empty) == len(rows) else
                           "Readable: " + ", ".join(r["subject"] for r in rows if r["status"] == "readable") + "."),
                          rows=rows, log={"direction": "out", "kinds": list(glpi.ITEMTYPES)},
                          fix="Check the profile's entity: the API user may be in an entity without assets." if empty and not errors else ""))
    finally:
        glpi.kill_session(url, app, session, timeout=timeout)
    return steps


def _inside_steps(db: Session) -> list[dict[str, Any]]:
    from app.glpi import backoff_remaining
    from app.glpi_sync import last_sync_entry
    from app.models import Asset

    wait = backoff_remaining()
    steps = [step("backoff", "Backoff", "Core", "warn" if wait > 0 else "ok",
                  f"ForgeSRE stopped calling GLPI for {int(wait)} s after a failure (the UI keeps working)." if wait > 0
                  else "ForgeSRE calls GLPI normally.")]
    last = last_sync_entry(db)
    if last is None:
        steps.append(step("sync", "Last sync", "never", "unknown", "GLPI was never synced.", fix="Discovery › Sync hosts › GLPI (./forgesre sources shows the last sync)"))
    else:
        tone = {"ok": "ok", "warn": "warn", "error": "bad"}.get(last.status, "warn")
        at = last.created_at.isoformat(timespec="minutes") if last.created_at else ""
        steps.append(step("sync", "Last sync", at[11:16] or "—", tone, f"{last.summary} ({at.replace('T', ' ')} UTC).",
                          fix="" if tone == "ok" else (last.detail or "")[:300]))
    linked = db.query(Asset).filter(Asset.glpi_id != "").count()
    steps.append(step("assets", "Inventory", f"{linked} linked", "ok" if linked else "unknown",
                      f"{linked} asset(s) in ForgeSRE come from GLPI." if linked else "No asset is linked to GLPI yet."))
    return steps


def _ticket_steps(db: Session, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Incident → GLPI ticket (optional): on or off, the right to create tickets, the last ticket calls."""
    from app.exchanges import list_exchanges
    from app.models import Incident
    from app.tickets import glpi_ready

    why = glpi_ready()
    if why:
        return [step("tickets", "Tickets", "tickets.glpi", "off", f"{why}.",
                     fix="Optional: ./forgesre settings set tickets.glpi.enabled true" if "off" in why else "")]
    right = profile.get("ticket")
    steps = [step("tickets", "Tickets", "on", "ok", "Analysts can open a GLPI ticket from an incident.")]
    if isinstance(right, int):
        can = bool(right & CREATE)
        steps.append(step("ticket_right", "Create right", str(profile.get("name") or "?"), "ok" if can else "bad",
                          "The API user's profile can create tickets." if can else "The API user's profile cannot create tickets.",
                          fix="" if can else "Give the profile Assistance › Ticket: Create and Update, and Followups: Add (or use Technician)."))
    calls = list_exchanges(db, "glpi", kinds=["POST Ticket", "GET Ticket"], limit=15)["rows"]
    failed = [c for c in calls if c["status"] != "ok"]
    linked = db.query(Incident).filter(Incident.ticket_ref.like("glpi:%")).count()
    rows = [{"at": c["at"], "status": c["status"], "error": "" if c["status"] == "ok" else c["summary"], "subject": c["summary"]} for c in calls]
    if not calls:
        steps.append(step("ticket_calls", "Ticket calls", f"{linked} ticket(s)", "unknown", "No ticket call yet.",
                          log={"direction": "out", "kinds": ["POST Ticket", "GET Ticket"]}))
    else:
        last = calls[0]
        steps.append(step("ticket_calls", "Ticket calls", f"{linked} ticket(s)", "bad" if last["status"] != "ok" else ("warn" if failed else "ok"),
                          f"Last: {last['summary']} ({last['status']}); {len(failed)} of the last {len(calls)} failed. {linked} incident(s) have a GLPI ticket.",
                          rows=rows, log={"direction": "out", "kinds": ["POST Ticket", "GET Ticket"]},
                          fix="Test: ./forgesre settings test ticket-glpi" if failed else ""))
    return steps


def check(db: Session) -> dict[str, Any]:
    """Settings → network → session → profile → assets, then backoff, last sync and linked assets."""
    from app.glpi import api_root

    url = settings.glpi_url
    have = {"GLPI_URL": bool(url), "GLPI_APP_TOKEN": bool(settings.glpi_app_token), "GLPI_USER_TOKEN": bool(settings.glpi_user_token)}
    missing = [k for k, v in have.items() if not v]
    out = [step("cfg", "Settings", "URL and tokens", "bad" if missing else "ok",
                f"Not set: {', '.join(missing)}." if missing else "GLPI_URL, GLPI_APP_TOKEN and GLPI_USER_TOKEN are set.",
                fix=f"./forgesre settings set-secret {missing[0]}" if missing else "")]
    profile: dict[str, Any] = {}
    if not missing and settings.glpi_enabled:
        net, ok = network_steps(api_root(url), what="GLPI")
        out += net
        if ok:
            out += _api_steps(profile)
    elif not missing:
        out.append(step("enabled", "Enabled", "inventory.glpi.enabled", "off", "GLPI is switched off (inventory.glpi.enabled: false)."))
    return {"lanes": [{"id": "out", "title": "ForgeSRE → GLPI (read-only)", "steps": out},
                      {"id": "in", "title": "Inside ForgeSRE (sync and inventory)", "steps": _inside_steps(db)},
                      {"id": "tickets", "title": "Incident → GLPI ticket", "steps": _ticket_steps(db, profile)}]}


register(Flow(
    name="glpi",
    label="GLPI",
    check=check,
    configured=lambda: bool(settings.glpi_enabled),
    log_words=("glpi",),
    about="How ForgeSRE reads assets from GLPI: settings, network, sign-in, the API user's rights, the assets it sees, the last sync, and incident tickets when they are on. The check never writes to GLPI.",
))
