"""Zabbix flow: the path between ForgeSRE and Zabbix in both directions, step by step.

``check_flow`` runs as a background job (it makes network calls): DNS, TCP and TLS to the Zabbix
frontend, the API and the token, what the token sees, then Zabbix's own side of the webhook path
read through the API (trigger action, media type, what Zabbix tried to send), and what ForgeSRE
received (from IntegrationExchange). Everything is read-only; every call has a timeout. A step that
cannot be seen with the token's role says so instead of failing.

``flow_context`` is what ForgeAI reads when someone asks about the flow on the Flow page.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.settings import settings

FLOW_KIND = "zabbix_flow"
CONNECT_TIMEOUT = 3.0
ALERT_STATUS = {"0": "not sent yet", "1": "sent", "2": "failed"}


def step(sid: str, label: str, sub: str, status: str, detail: str, *, fix: str = "", ms: int | None = None,
         log: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One block of the flow. status: ok, warn, bad, unknown, off. ``log`` filters the exchange log
    for this block; ``rows`` is extra detail shown in the big panel (e.g. Zabbix's send attempts)."""
    out: dict[str, Any] = {"id": sid, "label": label, "sub": sub, "status": status, "detail": detail, "fix": fix}
    if ms is not None:
        out["ms"] = ms
    if log:
        out["log"] = log
    if rows:
        out["rows"] = rows
    return out


def _timed(fn: Any, timeout: float) -> tuple[Any, float, BaseException | None]:
    """Run ``fn`` in a thread with a hard timeout (getaddrinfo has none of its own)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — reported as the step's error
            box["error"] = exc

    started = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    took = (time.monotonic() - started) * 1000
    if worker.is_alive():
        return None, took, TimeoutError(f"no answer in {timeout:.0f} s")
    return box.get("value"), took, box.get("error")


def _network_steps(url: str) -> tuple[list[dict[str, Any]], bool]:
    """DNS, TCP and (for https) TLS toward the Zabbix frontend. Returns the steps and whether the
    API can be tried at all."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    steps: list[dict[str, Any]] = []
    addrs, took, err = _timed(lambda: sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}), CONNECT_TIMEOUT)
    if err or not addrs:
        steps.append(step("dns", "DNS", host, "bad", f"{host} does not resolve: {err or 'no address'}.",
                          fix="Use the IP in ZABBIX_URL, or fix DNS on the ForgeSRE server (resolvectl status).", ms=int(took)))
        return steps, False
    steps.append(step("dns", "DNS", host, "ok", f"{host} → {', '.join(addrs[:3])}.", ms=int(took)))
    ip = addrs[0]

    def connect() -> None:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT):
            pass

    _, took, err = _timed(connect, CONNECT_TIMEOUT + 1)
    if err:
        steps.append(step("tcp", "TCP", f"{ip}:{port}", "bad", f"Cannot open {ip}:{port}: {err}.",
                          fix=f"A firewall between ForgeSRE and Zabbix, or the frontend is down. From the ForgeSRE server: nc -vz {ip} {port}", ms=int(took)))
        return steps, False
    steps.append(step("tcp", "TCP", f"{ip}:{port}", "ok", f"{ip}:{port} accepts connections.", ms=int(took)))
    if parts.scheme != "https":
        steps.append(step("tls", "TLS", "plain HTTP", "warn", "ZABBIX_URL is plain HTTP: the API token travels unencrypted.",
                          fix="Use the https:// address of the Zabbix frontend in ZABBIX_URL."))
        return steps, True

    def handshake() -> tuple[str, int]:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
            trusted = "trusted"
        except ssl.SSLCertVerificationError as exc:
            trusted = f"not trusted ({exc.verify_message})"
            cert = {}
        days = -1
        if cert.get("notAfter"):
            days = int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
        return trusted, days

    value, took, err = _timed(handshake, CONNECT_TIMEOUT + 1)
    if err:
        steps.append(step("tls", "TLS", "handshake", "bad", f"TLS handshake failed: {err}.", ms=int(took),
                          fix="Check the certificate and TLS settings of the Zabbix frontend."))
        return steps, False
    trusted, days = value
    if trusted != "trusted":
        steps.append(step("tls", "TLS", "certificate", "bad", f"The certificate is {trusted}; API calls are refused.", ms=int(took),
                          fix="Use a certificate from a CA the server trusts, or add the CA to the ForgeSRE image (docs/zabbix.md)."))
        return steps, False
    status = "ok" if days > 30 else "warn"
    steps.append(step("tls", "TLS", "certificate", status, f"Trusted; expires in {days} day(s)." if days >= 0 else "Trusted.", ms=int(took),
                      fix="" if status == "ok" else "Renew the Zabbix frontend certificate."))
    return steps, True


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
                f"{settings_port()} proto tcp; check forge_url in the media type.")
    if "401" in low:
        return "forge_token in the media type differs from ZABBIX_WEBHOOK_TOKEN on ForgeSRE."
    if "422" in low:
        return "The body lacks the trigger name or host: compare the media type parameters with docs/zabbix.md section 7."
    if "503" in low:
        return "ZABBIX_WEBHOOK_TOKEN is empty on ForgeSRE: ./forgesre settings set-secret ZABBIX_WEBHOOK_TOKEN, then ./forgesre update."
    return "See the error text; docs/zabbix.md section 13."


def settings_port() -> str:
    import os

    return os.environ.get("FORGESRE_HTTP_PORT") or "8080"


def _received_steps(db: Session) -> list[dict[str, Any]]:
    """What ForgeSRE received on /api/v1/webhooks/zabbix, from the exchange log."""
    from app.models import Incident, IntegrationExchange

    since = datetime.now(timezone.utc) - timedelta(days=1)
    q = db.query(IntegrationExchange).filter(IntegrationExchange.integration == "zabbix", IntegrationExchange.direction == "in")
    last = q.order_by(IntegrationExchange.id.desc()).first()
    recent = q.filter(IntegrationExchange.at >= since).count()
    log = {"direction": "in", "kinds": ["webhook"]}
    steps = []
    if not settings.zabbix_webhook_token:
        steps.append(step("network", "Network", f"→ :{settings_port()}", "unknown", "Nothing can arrive: the webhook is off.", log=log))
        steps.append(step("webhook", "Webhook", "/webhooks/zabbix", "bad", "ZABBIX_WEBHOOK_TOKEN is empty on ForgeSRE.", log=log,
                          fix="./forgesre settings set-secret ZABBIX_WEBHOOK_TOKEN, put the same value in forge_token of the media type, then ./forgesre update."))
    elif last is None:
        steps.append(step("network", "Network", f"→ :{settings_port()}", "warn", "Nothing has arrived from Zabbix yet.", log=log,
                          fix=f"From the Zabbix server: curl -X POST http://<FORGE-IP>:{settings_port()}/api/v1/webhooks/zabbix — any HTTP answer shows the port is open. Or use Wait for a test below."))
        steps.append(step("webhook", "Webhook", "/webhooks/zabbix", "unknown", "No call yet.", log=log))
    else:
        at = last.at if last.at.tzinfo else last.at.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - at).total_seconds() / 3600
        steps.append(step("network", "Network", f"{last.peer or '?'} → :{settings_port()}", "ok" if age_h < 24 else "warn",
                          f"Last call {at.isoformat()[:16].replace('T', ' ')} UTC from {last.peer or 'unknown'}; {recent} in the last 24 h.", log=log))
        meaning = {"ok": ("ok", "Accepted."), "test": ("ok", "Media type Test: URL and token are right."),
                   "refused": ("bad", last.summary), "error": ("warn", last.summary)}
        status, text = meaning.get(last.status, ("warn", last.summary))
        steps.append(step("webhook", "Webhook", f"HTTP {last.http_status}", status, f"{text} {last.summary if last.status in {'ok', 'test'} else ''}".strip(), log=log,
                          fix=_send_fix(str(last.http_status)) if status != "ok" else ""))
    incident = db.query(Incident).filter(Incident.source == "zabbix").order_by(Incident.started_at.desc()).first()
    if incident is None:
        steps.append(step("incident", "Incident", "source zabbix", "unknown", "No incident from Zabbix yet."))
    else:
        asset = incident.asset.hostname if incident.asset is not None else "no asset (run Sync hosts)"
        steps.append(step("incident", "Incident", incident.number.split("_")[0], "ok" if incident.asset is not None else "warn",
                          f"Last: {incident.title} on {asset}, {incident.status.lower()}.",
                          fix="" if incident.asset is not None else "Discovery › Zabbix › Sync hosts, so the host becomes an asset."))
    return steps


def check_flow(db: Session) -> dict[str, Any]:
    """Every step in both directions. Job handler for FLOW_KIND; returns the result stored on the job."""
    out_lane: list[dict[str, Any]] = []
    in_lane: list[dict[str, Any]] = []
    configured = bool(settings.zabbix_url and settings.zabbix_token)
    out_lane.append(step("config", "Settings", "URL and token", "ok" if configured else "bad",
                         f"ZABBIX_URL {settings.zabbix_url or 'empty'}; API token {'set' if settings.zabbix_token else 'empty'}.",
                         fix="" if configured else "Put ZABBIX_URL and ZABBIX_API_TOKEN in secrets/secrets.env (./forgesre settings set-secret), then ./forgesre update."))
    zabbix_side: list[dict[str, Any]] = []
    if configured:
        net, reachable = _network_steps(settings.zabbix_url)
        out_lane += net
        if reachable:
            api, _ = _api_steps()
            out_lane += api
            if any(s["id"] == "token" and s["status"] == "ok" for s in api):
                zabbix_side, _ = _zabbix_side_steps()
    if not zabbix_side:
        zabbix_side = [step("media", "Media type", "Webhook", "unknown", "Zabbix's side is read through the API; the API path above has to work first.")]
    in_lane = zabbix_side + _received_steps(db)
    return {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lanes": [{"id": "in", "title": "Zabbix → ForgeSRE (alarms, webhook)", "steps": in_lane},
                      {"id": "out", "title": "ForgeSRE → Zabbix (API, read-only)", "steps": out_lane}]}


def last_flow(db: Session) -> dict[str, Any]:
    """The newest flow check job: its status and, when done, its result."""
    from app.models import Job

    row = db.query(Job).filter(Job.kind == FLOW_KIND).order_by(Job.id.desc()).first()
    if row is None:
        return {"status": "none", "result": None}
    done = row if row.status == "done" else db.query(Job).filter(Job.kind == FLOW_KIND, Job.status == "done").order_by(Job.id.desc()).first()
    return {"status": row.status, "job": row.id, "error": row.error or "", "result": (done.payload or {}).get("result") if done else None,
            "queued_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
            "watch": bool(settings.zabbix_flow_watch), "ai": last_ai(db)}


def flow_context(db: Session, integration: str = "zabbix") -> dict[str, Any]:
    """What ForgeAI reads in a Flow chat: the last check (each block) and the recent exchanges, short."""
    from app.exchanges import list_exchanges

    flow = (last_flow(db).get("result") or {}) if integration == "zabbix" else {}
    blocks = [{"lane": lane["id"], "block": s["label"], "status": s["status"], "detail": s["detail"], "fix": s.get("fix", ""),
               **({"zabbix_send_attempts": s["rows"][:8]} if s.get("rows") else {})}
              for lane in flow.get("lanes", []) for s in lane["steps"]]
    exchanges = [{k: r[k] for k in ("at", "direction", "kind", "peer", "status", "http_status", "ms", "summary")}
                 for r in list_exchanges(db, integration, limit=40)["rows"]]
    logs = core_log_lines(30) if integration == "zabbix" else {"lines": []}
    return {"integration": integration, "flow_checked_at": flow.get("checked_at"), "flow_blocks": blocks, "recent_exchanges": exchanges,
            "core_log_lines": [line[-300:] for line in logs["lines"]], "ports": {"forgesre_http": settings_port()}}


# --- Watch: automatic checks, notice in the journal, ForgeAI analysis ------------------------------

WATCH_EVERY = 15 * 60
AI_KIND = "zabbix_flow_ai"
LOG_WORDS = ("zabbix",)


def core_log_lines(limit: int = 60) -> dict[str, Any]:
    """The last Core container log lines that mention Zabbix (through docker-ro, 5 s timeout, masked).
    ``error`` says why there are none (docker-ro off, no container)."""
    import httpx

    from app.containers import logs

    try:
        out = logs("core", 1000)
    except LookupError as exc:
        return {"lines": [], "error": str(exc)}
    except httpx.HTTPError as exc:
        return {"lines": [], "error": f"docker-ro does not answer ({exc.__class__.__name__}): docker compose up -d docker-ro"}
    lines = [line for line in out.get("lines") or [] if any(w in line.lower() for w in LOG_WORDS)]
    return {"lines": lines[-limit:], "error": ""}


def _broken(result: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Blocks that are bad, by lane and id."""
    return {f"{lane['id']}:{s['id']}": s for lane in (result or {}).get("lanes", []) for s in lane["steps"] if s["status"] == "bad"}


def previous_result(db: Session, before_job: int | None = None) -> dict[str, Any] | None:
    from app.models import Job

    q = db.query(Job).filter(Job.kind == FLOW_KIND, Job.status == "done")
    if before_job:
        q = q.filter(Job.id < before_job)
    row = q.order_by(Job.id.desc()).first()
    return (row.payload or {}).get("result") if row is not None else None


def notice_changes(db: Session, before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """Journal a block that broke (an error the UI shows in the Settings badge) and one that works
    again; queue a ForgeAI analysis of the new break when ForgeAI is on. Returns the newly broken labels."""
    from app.jobs import enqueue
    from app.journal import report

    old, new = _broken(before), _broken(after)
    broke = [new[k] for k in new if k not in old]
    healed = [old[k] for k in old if k not in new]
    for s in broke:
        report(db, "zabbix", "flow", "error", summary=f"Zabbix flow broke at {s['label']}: {s['detail']}"[:300],
               detail=f"{s.get('fix', '')}\nSettings › Integrations › Zabbix › Flow and log", object_type="integration", object_id="zabbix")
    for s in healed:
        report(db, "zabbix", "flow", "ok", summary=f"Zabbix flow works again at {s['label']}", object_type="integration", object_id="zabbix")
    if broke and settings.ai_enabled and settings.llm_url and settings.zabbix_flow_ai:
        question = ("These blocks just broke: " + "; ".join(f"{s['label']} ({s['detail']})" for s in broke)
                    + ". Where does the flow break, why, and what should the operator do first?")
        enqueue(db, AI_KIND, "zabbix", object_type="integration", payload={"question": question[:900]})
    db.commit()
    return [s["label"] for s in broke]


def run_ai(db: Session, question: str) -> dict[str, Any]:
    """Job handler (LLM lane): ForgeAI's analysis of a new break, kept as a logged call."""
    from app import forgeai

    call = forgeai.run(db, "flow", None, actor="system", question=question, flow="zabbix")
    return {"call_id": call.id, "status": call.status, "reason": call.reason or ""}


def last_ai(db: Session) -> dict[str, Any] | None:
    """The newest automatic analysis: when, the answer, or why there is none."""
    from app.models import ForgeAiCall, Job

    row = db.query(Job).filter(Job.kind == AI_KIND).order_by(Job.id.desc()).first()
    if row is None:
        return None
    result = (row.payload or {}).get("result") or {}
    out: dict[str, Any] = {"status": row.status, "at": row.created_at.isoformat() if row.created_at else None,
                           "question": (row.payload or {}).get("question", ""), "answer": "", "call_id": result.get("call_id")}
    call = db.get(ForgeAiCall, int(result["call_id"])) if result.get("call_id") else None
    if call is not None:
        out["answer"] = str((call.answer or {}).get("answer") or "") if call.status in {"trial", "used"} else f"ForgeAI could not answer: {call.reason or call.status}"
        out["model"] = call.model or ""
    return out


def watch_due(db: Session, now: datetime | None = None) -> bool:
    """A check is due when Zabbix is set up, watching is on, and the last check is older than WATCH_EVERY."""
    from app.models import Job

    if not (settings.zabbix_url and settings.zabbix_token) or not settings.zabbix_flow_watch:
        return False
    row = db.query(Job).filter(Job.kind == FLOW_KIND).order_by(Job.id.desc()).first()
    if row is None or row.created_at is None:
        return True
    at = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - at).total_seconds() >= WATCH_EVERY
