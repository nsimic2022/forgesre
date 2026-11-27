"""Zabbix flow: the path between ForgeSRE and Zabbix in both directions (registered in app.flows).

ForgeSRE → Zabbix: settings, DNS, TCP, TLS, API version, token, rights, problems. Zabbix → ForgeSRE:
Zabbix's own side read through the API (media type, trigger action, what Zabbix tried to send),
then what ForgeSRE received on its webhook and the last incident. Read-only; every call has a
timeout; a step the token's role cannot see says so instead of failing.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.flows import Flow, forgesre_port, network_steps, received_steps, register, step
from app.settings import settings

ALERT_STATUS = {"0": "not sent yet", "1": "sent", "2": "failed"}


def _api(method: str, params: dict[str, Any]) -> tuple[Any, int, str]:
    """One read-only call, forced past the backoff (this is an explicit check); (result, ms, error)."""
    from app.zabbix import ZabbixError, call

    started = time.monotonic()
    try:
        result = call(method, params, url=settings.zabbix_url, token=settings.zabbix_token, timeout=settings.zabbix_timeout, force=True)
    except ZabbixError as exc:
        return None, int((time.monotonic() - started) * 1000), str(exc)
    return result, int((time.monotonic() - started) * 1000), ""


def _not_visible(err: str) -> bool:
    low = err.lower()
    return "permission" in low or "no permissions" in low or "not authorized" in low or "access denied" in low


def _api_steps() -> tuple[list[dict[str, Any]], list[str]]:
    """API version, token, rights, open problems. Returns steps and the ids of the ForgeSRE media types."""
    from app.zabbix import ZabbixError, reset_state, server_version

    steps: list[dict[str, Any]] = []
    reset_state()
    started = time.monotonic()
    try:
        version = server_version(settings.zabbix_url, settings.zabbix_timeout, force=True)
    except ZabbixError as exc:
        steps.append(step("api", "API", "api_jsonrpc.php", "bad", str(exc), ms=int((time.monotonic() - started) * 1000),
                          fix="Check ZABBIX_URL: it is the frontend address that serves api_jsonrpc.php.", log={"direction": "out", "kinds": ["apiinfo.version"]}))
        return steps, []
    steps.append(step("api", "API", "api_jsonrpc.php", "ok", f"Zabbix {version[0]}.{version[1]} answered.", ms=int((time.monotonic() - started) * 1000),
                      log={"direction": "out", "kinds": ["apiinfo.version"]}))
    hosts, ms, err = _api("host.get", {"countOutput": True})
    if err:
        steps.append(step("token", "Token", "forgesre-ro", "bad", err, ms=ms, log={"direction": "out", "kinds": ["host.get"]},
                          fix="Make a new API token for forgesre-ro (Users › API tokens), then ./forgesre settings set-secret ZABBIX_API_TOKEN."))
        return steps, []
    auth = "Bearer header" if version >= (6, 4) else "auth field"
    steps.append(step("token", "Token", "forgesre-ro", "ok", f"Accepted ({auth}).", ms=ms, log={"direction": "out", "kinds": ["host.get"]}))
    groups, ms, err = _api("hostgroup.get", {"output": ["name"], "with_hosts": True})
    count = int(str(hosts or "0") or 0)
    if err and not _not_visible(err):
        steps.append(step("rights", "Rights", "host groups", "warn", err, ms=ms))
    else:
        names = sorted(g.get("name", "") for g in (groups or []) if isinstance(g, dict))
        status = "ok" if count else "warn"
        steps.append(step("rights", "Rights", "host groups", status,
                          f"{count} host(s) in {len(names)} group(s): {', '.join(names[:8])}{' …' if len(names) > 8 else ''}." if names else f"{count} host(s) readable.",
                          ms=ms, fix="" if count else "Give the forgesre-ro user group Read on the host groups to import (Users › User groups › Host permissions)."))
    problems, ms, err = _api("problem.get", {"countOutput": True})
    steps.append(step("problems", "Problems", "problem.get", "warn" if err else "ok", err or f"{problems} open problem(s) visible.", ms=ms,
                      log={"direction": "out", "kinds": ["problem.get"]}))
    return steps, []


def _zabbix_side_steps() -> tuple[list[dict[str, Any]], list[str]]:
    """Zabbix's side of the webhook path, read through the API: the ForgeSRE media type, the trigger
    action that uses it and the last send attempts. Hidden objects give 'unknown' with what to allow."""
    steps: list[dict[str, Any]] = []
    allow = ("The token's role cannot read this. To see it here, give forgesre-ro a role with API access to "
             "mediatype.get, action.get and alert.get (still read-only), or check it in Zabbix: Alerts › Media types.")
    media, ms, err = _api("mediatype.get", {"output": ["mediatypeid", "name", "type", "status"], "filter": {"type": 4}})
    ours = [m for m in (media or []) if isinstance(m, dict) and "forge" in str(m.get("name", "")).lower()]
    if err or media is None:
        steps.append(step("media", "Media type", "Webhook", "unknown", err or "No answer.", ms=ms, fix=allow, log={"direction": "out", "kinds": ["mediatype.get"]}))
        ids: list[str] = []
    elif not ours:
        steps.append(step("media", "Media type", "Webhook", "bad" if media else "unknown",
                          "No webhook media type named ForgeSRE is visible." if media else "No webhook media types are visible to this token.",
                          ms=ms, fix="Create it as in docs/zabbix.md section 7 (name it ForgeSRE)." if media else allow))
        ids = []
    else:
        on = [m for m in ours if str(m.get("status")) == "0"]
        names = ", ".join(m.get("name", "") for m in ours)
        steps.append(step("media", "Media type", names, "ok" if on else "bad", f"{names}: {'enabled' if on else 'disabled'}.", ms=ms,
                          fix="" if on else "Enable it in Alerts › Media types.", log={"direction": "out", "kinds": ["mediatype.get"]}))
        ids = [str(m.get("mediatypeid")) for m in ours]
    if ids:
        actions, ms, err = _api("action.get", {"output": ["name", "status"], "filter": {"eventsource": 0}, "mediatypeids": ids})
        if err:
            steps.append(step("action", "Trigger action", "Alerts › Actions", "unknown", err, ms=ms, fix=allow))
        elif not actions:
            steps.append(step("action", "Trigger action", "Alerts › Actions", "bad", "No trigger action sends to the ForgeSRE media type.", ms=ms,
                              fix="Create the action with problem and recovery operations (docs/zabbix.md section 8)."))
        else:
            on = [a for a in actions if str(a.get("status")) == "0"]
            steps.append(step("action", "Trigger action", ", ".join(a.get("name", "") for a in actions[:2]), "ok" if on else "bad",
                              f"{len(on)} of {len(actions)} enabled.", ms=ms, fix="" if on else "Enable the action in Alerts › Actions.",
                              log={"direction": "out", "kinds": ["action.get"]}))
        sent, ms, err = _api("alert.get", {"output": ["clock", "status", "error", "retries", "sendto", "subject"], "mediatypeids": ids,
                                           "sortfield": "clock", "sortorder": "DESC", "limit": 20})
        if err:
            steps.append(step("sent", "Zabbix sent", "alert.get", "unknown", err, ms=ms, fix=allow))
        else:
            rows = [{"at": datetime.fromtimestamp(int(a.get("clock") or 0), timezone.utc).isoformat(), "status": ALERT_STATUS.get(str(a.get("status")), str(a.get("status"))),
                     "error": str(a.get("error") or "")[:300], "retries": a.get("retries"), "subject": str(a.get("subject") or "")[:120]} for a in (sent or [])]
            if not rows:
                steps.append(step("sent", "Zabbix sent", "alert.get", "warn", "Zabbix has not tried to send anything to ForgeSRE yet.", ms=ms,
                                  fix="Make a test problem, or press Test on the media type (docs/zabbix.md 9.2)."))
            else:
                last = rows[0]
                failed = [r for r in rows if r["status"] == "failed"]
                status = "bad" if last["status"] == "failed" else ("warn" if failed else "ok")
                text = f"Last attempt {last['at'][11:16]} UTC: {last['status']}" + (f" — {last['error']}" if last["error"] else "") + "."
                steps.append(step("sent", "Zabbix sent", f"{len(rows)} recent", status, text, ms=ms, rows=rows,
                                  fix=_send_fix(last["error"]) if status != "ok" else "", log={"direction": "in", "kinds": ["webhook"]}))
    return steps, ids


def _send_fix(error: str) -> str:
    low = error.lower()
    if "couldn't connect" in low or "connection refused" in low or "timed out" in low or "timeout" in low:
        return ("The Zabbix server cannot reach ForgeSRE. On ForgeSRE: sudo ufw allow from <ZABBIX-SERVER-IP> to any port "
                f"{forgesre_port()} proto tcp; check forge_url in the media type.")
    if "401" in low:
        return "forge_token in the media type differs from ZABBIX_WEBHOOK_TOKEN on ForgeSRE."
    if "422" in low:
        return "The body lacks the trigger name or host: compare the media type parameters with docs/zabbix.md section 7."
    if "503" in low:
        return "ZABBIX_WEBHOOK_TOKEN is empty on ForgeSRE: ./forgesre settings set-secret ZABBIX_WEBHOOK_TOKEN, then ./forgesre update."
    return "See the error text; docs/zabbix.md section 13."


def check(db: Session) -> dict[str, Any]:
    """Every step in both directions (the job handler of flow "zabbix")."""
    out_lane: list[dict[str, Any]] = []
    configured = bool(settings.zabbix_url and settings.zabbix_token)
    out_lane.append(step("config", "Settings", "URL and token", "ok" if configured else "bad",
                         f"ZABBIX_URL {settings.zabbix_url or 'empty'}; API token {'set' if settings.zabbix_token else 'empty'}.",
                         fix="" if configured else "Put ZABBIX_URL and ZABBIX_API_TOKEN in secrets/secrets.env (./forgesre settings set-secret), then ./forgesre update."))
    zabbix_side: list[dict[str, Any]] = []
    if configured:
        net, reachable = network_steps(settings.zabbix_url, what="the Zabbix frontend")
        out_lane += net
        if reachable:
            api, _ = _api_steps()
            out_lane += api
            if any(s["id"] == "token" and s["status"] == "ok" for s in api):
                zabbix_side, _ = _zabbix_side_steps()
    if not zabbix_side:
        zabbix_side = [step("media", "Media type", "Webhook", "unknown", "Zabbix's side is read through the API; the API path above has to work first.")]
    received = received_steps(db, "zabbix", sender="the Zabbix server (media type)", endpoint="/api/v1/webhooks/zabbix",
                              token_name="ZABBIX_WEBHOOK_TOKEN", token_set=bool(settings.zabbix_webhook_token), incident_source="zabbix")
    return {"lanes": [{"id": "in", "title": "Zabbix → ForgeSRE (alarms, webhook)", "steps": zabbix_side + received},
                      {"id": "out", "title": "ForgeSRE → Zabbix (API, read-only)", "steps": out_lane}]}


FLOW = register(Flow(
    name="zabbix", label="Zabbix", check=check, configured=lambda: bool(settings.zabbix_url and settings.zabbix_token),
    log_words=("zabbix",),
    about="How alarms come in from Zabbix and how ForgeSRE reads Zabbix, step by step.",
))
