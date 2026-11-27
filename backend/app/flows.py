"""Integration flows: the path between ForgeSRE and an outside system, block by block.

Each flow (Zabbix, Prometheus → Alertmanager, notifications, GLPI, …) registers a ``Flow`` with a
``check(db)`` that returns lanes of blocks. This module runs the check as a background job
(``flow_check``, object_id = flow name), keeps the last result, watches every configured flow every
15 minutes, writes a block that breaks to the journal (the Settings badge) and, with ForgeAI on,
queues an analysis (``flow_ai``, LLM lane). Everything here is read-only.

A flow may also check one object (an asset for SNMP): ``target_check(db, target)``; its job's
object_id is ``name@target``. Per-object checks are run on request only: not watched, not journaled.

``flow_context`` is what ForgeAI reads in a Flow chat (topic ``flow:<name>``).
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.settings import settings

CHECK_KIND = "flow_check"
AI_KIND = "flow_ai"
WATCH_EVERY = 15 * 60
CONNECT_TIMEOUT = 3.0


@dataclass(frozen=True)
class Flow:
    """One integration's flow. ``check`` returns {"lanes": [...]}; ``configured`` says whether the
    integration is set up (only those are watched); ``log_words`` pick Core log lines for the page
    and ForgeAI; ``services`` are compose services whose own logs belong to this flow."""

    name: str
    label: str
    check: Callable[[Session], dict[str, Any]]
    configured: Callable[[], bool]
    log_words: tuple[str, ...] = ()
    services: tuple[str, ...] = ("core",)
    about: str = ""
    extra_context: Callable[[Session], dict[str, Any]] | None = field(default=None)
    target_check: Callable[[Session, str], dict[str, Any]] | None = field(default=None)
    target_label: Callable[[Session, str], str] | None = field(default=None)


_REGISTRY: dict[str, Flow] = {}
_LOADED = False


def register(flow: Flow) -> Flow:
    _REGISTRY[flow.name] = flow
    return flow


def flows() -> dict[str, Flow]:
    """Every registered flow (imports the flow modules on first use, in the order of the map)."""
    global _LOADED
    if not _LOADED:
        _load()
        _LOADED = True
    order = list(FLOW_ORDER) + [n for n in _REGISTRY if n not in FLOW_ORDER]
    return {n: _REGISTRY[n] for n in order if n in _REGISTRY}


def get(name: str) -> Flow:
    found = flows().get(name)
    if found is None:
        raise LookupError(f"no flow named {name!r}")
    return found


# Order on the integration map: alarm intake first, then the systems ForgeSRE reads or writes.
FLOW_ORDER = ("prometheus", "zabbix", "librenms", "snmp", "syslog", "kubernetes", "glpi", "netbox", "oxidized", "notifications", "forgeai", "mcp")
TARGET_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _load() -> None:
    # Each module calls register() at import.
    from app import (flow_forgeai, flow_glpi, flow_kubernetes, flow_librenms, flow_mcp, flow_netbox,  # noqa: F401
                     flow_notifications, flow_oxidized, flow_prometheus, flow_snmp, flow_syslog, zabbix_flow)


def job_key(name: str, target: str | None = None) -> str:
    """The job object_id of a check: the flow name, or ``name@target`` for one object."""
    flow = get(name)
    if not target:
        return name
    if flow.target_check is None or not TARGET_RE.match(target):
        raise LookupError(f"flow {name!r} cannot check {target!r}")
    return f"{name}@{target}"


def split_key(key: str) -> tuple[str, str | None]:
    name, _, target = key.partition("@")
    return name, target or None


# --- Blocks and shared network steps -----------------------------------------------------------

def step(sid: str, label: str, sub: str, status: str, detail: str, *, fix: str = "", ms: int | None = None,
         log: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One block of a flow. status: ok, warn, bad, unknown, off. ``log`` filters the exchange log for
    this block; ``rows`` is extra detail shown in the big panel (e.g. Zabbix's send attempts)."""
    out: dict[str, Any] = {"id": sid, "label": label, "sub": sub, "status": status, "detail": detail, "fix": fix}
    if ms is not None:
        out["ms"] = ms
    if log:
        out["log"] = log
    if rows:
        out["rows"] = rows
    return out


def timed(fn: Any, timeout: float) -> tuple[Any, float, BaseException | None]:
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


def network_steps(url: str, *, what: str = "the service", tls: bool | None = None, prefix: str = "") -> tuple[list[dict[str, Any]], bool]:
    """DNS, TCP and (for https) TLS toward ``url``. Returns the steps and whether the API can be tried.
    ``tls=False`` skips the TLS block (SMTP checks STARTTLS when it signs in); ``prefix`` keeps block
    ids unique when one lane has several targets."""
    steps, ok = _network_steps(url, what=what, tls=tls)
    if prefix:
        for s in steps:
            s["id"] = f"{prefix}{s['id']}"
    return steps, ok


def _network_steps(url: str, *, what: str, tls: bool | None) -> tuple[list[dict[str, Any]], bool]:
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    steps: list[dict[str, Any]] = []
    addrs, took, err = timed(lambda: sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}), CONNECT_TIMEOUT)
    if err or not addrs:
        steps.append(step("dns", "DNS", host, "bad", f"{host} does not resolve: {err or 'no address'}.",
                          fix="Use the IP in the URL, or fix DNS on the ForgeSRE server (resolvectl status).", ms=int(took)))
        return steps, False
    steps.append(step("dns", "DNS", host, "ok", f"{host} → {', '.join(addrs[:3])}.", ms=int(took)))
    ip = addrs[0]

    def connect() -> None:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT):
            pass

    _, took, err = timed(connect, CONNECT_TIMEOUT + 1)
    if err:
        steps.append(step("tcp", "TCP", f"{ip}:{port}", "bad", f"Cannot open {ip}:{port}: {err}.",
                          fix=f"A firewall between ForgeSRE and {what}, or it is down. From the ForgeSRE server: nc -vz {ip} {port}", ms=int(took)))
        return steps, False
    steps.append(step("tcp", "TCP", f"{ip}:{port}", "ok", f"{ip}:{port} accepts connections.", ms=int(took)))
    if tls is False:
        return steps, True
    if parts.scheme != "https":
        local = ip.startswith("127.") or ip == "::1"
        steps.append(step("tls", "TLS", "plain HTTP", "ok" if local else "warn",
                          "Plain HTTP on this host only." if local else "Plain HTTP: tokens travel unencrypted.",
                          fix="" if local else f"Use the https:// address of {what}."))
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

    value, took, err = timed(handshake, CONNECT_TIMEOUT + 1)
    if err:
        steps.append(step("tls", "TLS", "handshake", "bad", f"TLS handshake failed: {err}.", ms=int(took),
                          fix=f"Check the certificate and TLS settings of {what}."))
        return steps, False
    trusted, days = value
    if trusted != "trusted":
        steps.append(step("tls", "TLS", "certificate", "bad", f"The certificate is {trusted}; calls are refused.", ms=int(took),
                          fix="Use a certificate from a CA the server trusts, or add the CA to the ForgeSRE image."))
        return steps, False
    status = "ok" if days > 30 else "warn"
    steps.append(step("tls", "TLS", "certificate", status, f"Trusted; expires in {days} day(s)." if days >= 0 else "Trusted.", ms=int(took),
                      fix="" if status == "ok" else f"Renew the certificate of {what}."))
    return steps, True


def forgesre_port() -> str:
    return os.environ.get("FORGESRE_HTTP_PORT") or "8080"


def received_steps(db: Session, integration: str, *, sender: str, endpoint: str, token_name: str, token_set: bool,
                   incident_source: str) -> list[dict[str, Any]]:
    """Network → Webhook → Incident, from what ForgeSRE logged on its webhook (shared by the flows
    whose alarms come in over HTTP)."""
    from app.models import Incident, IntegrationExchange

    since = datetime.now(timezone.utc) - timedelta(days=1)
    q = db.query(IntegrationExchange).filter(IntegrationExchange.integration == integration, IntegrationExchange.direction == "in")
    last = q.order_by(IntegrationExchange.id.desc()).first()
    recent = q.filter(IntegrationExchange.at >= since).count()
    log = {"direction": "in", "kinds": ["webhook"]}
    port = forgesre_port()
    steps = []
    if not token_set:
        steps.append(step("network", "Network", f"→ :{port}", "unknown", "Nothing can arrive: the webhook is off.", log=log))
        steps.append(step("webhook", "Webhook", endpoint, "bad", f"{token_name} is empty on ForgeSRE.", log=log,
                          fix=f"./forgesre settings set-secret {token_name}, use the same value in {sender}, then ./forgesre update."))
    elif last is None:
        steps.append(step("network", "Network", f"→ :{port}", "warn", f"Nothing has arrived from {sender} yet.", log=log,
                          fix=f"From {sender}: curl -X POST http://<FORGE-IP>:{port}{endpoint} — any HTTP answer shows the port is open."))
        steps.append(step("webhook", "Webhook", endpoint, "unknown", "No call yet.", log=log))
    else:
        at = last.at if last.at.tzinfo else last.at.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - at).total_seconds() / 3600
        steps.append(step("network", "Network", f"{last.peer or '?'} → :{port}", "ok" if age_h < 24 else "warn",
                          f"Last call {at.isoformat()[:16].replace('T', ' ')} UTC from {last.peer or 'unknown'}; {recent} in the last 24 h.", log=log))
        meaning = {"ok": ("ok", "Accepted."), "test": ("ok", "Test call: URL and token are right."),
                   "refused": ("bad", last.summary), "error": ("warn", last.summary)}
        status, text = meaning.get(last.status, ("warn", last.summary))
        fix = {401: f"The token {sender} sends differs from {token_name} on ForgeSRE.",
               503: f"{token_name} is empty on ForgeSRE: ./forgesre settings set-secret {token_name}, then ./forgesre update.",
               422: "The body lacks required fields: compare the sender's payload with the docs."}.get(last.http_status, "")
        steps.append(step("webhook", "Webhook", f"HTTP {last.http_status}", status,
                          f"{text} {last.summary if last.status in {'ok', 'test'} else ''}".strip(), log=log, fix=fix if status != "ok" else ""))
    incident = db.query(Incident).filter(Incident.source == incident_source).order_by(Incident.started_at.desc()).first()
    if incident is None:
        steps.append(step("incident", "Incident", f"source {incident_source}", "unknown", f"No incident from {sender} yet."))
    else:
        asset = incident.asset.hostname if incident.asset is not None else "no asset"
        steps.append(step("incident", "Incident", incident.number.split("_")[0], "ok" if incident.asset is not None else "warn",
                          f"Last: {incident.title} on {asset}, {incident.status.lower()}.",
                          fix="" if incident.asset is not None else "Add the host to the inventory (Discovery › Sync hosts), so alarms match an asset."))
    return steps


def journal_step(db: Session, sid: str, label: str, module: str, action: str, *, never: str, fix: str = "") -> dict[str, Any]:
    """The newest journal line of ``module``/``action`` (a sync, a health note) as one block."""
    from app.models import JournalEntry

    last = (db.query(JournalEntry).filter(JournalEntry.module == module, JournalEntry.action == action)
            .order_by(JournalEntry.id.desc()).first())
    if last is None:
        return step(sid, label, "never", "unknown", never, fix=fix)
    tone = {"ok": "ok", "warn": "warn", "error": "bad"}.get(last.status, "warn")
    at = last.created_at.isoformat(timespec="minutes") if last.created_at else ""
    return step(sid, label, at[11:16] or "—", tone, f"{last.summary} ({at.replace('T', ' ')} UTC).",
                fix="" if tone == "ok" else ((last.detail or "")[:300] or fix))


def http_step(sid: str, label: str, url: str, *, headers: dict[str, str] | None = None, timeout: float = 3.0,
              ok_text: str = "Answers.", fix: str = "", verify: Any = True) -> tuple[dict[str, Any], Any]:
    """GET ``url`` as one block: (step, the JSON or text answer or None). Headers are never shown."""
    import httpx

    started = time.monotonic()
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, verify=verify, follow_redirects=True)
    except httpx.HTTPError as exc:
        return step(sid, label, urlsplit(url).path or "/", "bad", f"No answer: {exc.__class__.__name__}.",
                    ms=int((time.monotonic() - started) * 1000), fix=fix), None
    ms = int((time.monotonic() - started) * 1000)
    if r.status_code >= 400:
        return step(sid, label, urlsplit(url).path or "/", "bad", f"HTTP {r.status_code}.", ms=ms, fix=fix), None
    try:
        body: Any = r.json()
    except ValueError:
        body = r.text
    return step(sid, label, urlsplit(url).path or "/", "ok", ok_text, ms=ms), body


# --- Running and keeping results ---------------------------------------------------------------

def run_check(db: Session, key: str) -> dict[str, Any]:
    """Job handler: run one flow's check (``name`` or ``name@target``) and stamp it."""
    name, target = split_key(key)
    flow = get(name)
    if target:
        if flow.target_check is None:
            raise LookupError(f"flow {name!r} has no per-object check")
        result = flow.target_check(db, target)
    else:
        result = flow.check(db)
    result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _jobs(db: Session, kind: str, name: str) -> Any:
    from app.models import Job

    return db.query(Job).filter(Job.kind == kind, Job.object_id == name)


def watch_on() -> bool:
    return bool(settings.flows_watch)


def ai_on() -> bool:
    return bool(settings.flows_ai and settings.ai_enabled and settings.llm_url)


def last_ai(db: Session, name: str) -> dict[str, Any] | None:
    """The newest automatic analysis of this flow: when, the answer, or why there is none."""
    from app.models import ForgeAiCall, Job

    row = _jobs(db, AI_KIND, name).order_by(Job.id.desc()).first()
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


def last_flow(db: Session, name: str, target: str | None = None) -> dict[str, Any]:
    """The newest check of this flow (or of one object in it): its job status and the newest finished result."""
    from app.models import Job

    flow = get(name)
    key = job_key(name, target)
    base: dict[str, Any] = {"name": name, "label": flow.label, "about": flow.about, "watch": watch_on() and not target,
                            "configured": flow.configured(), "targets": flow.target_check is not None}
    if target:
        base["target"] = target
        base["target_label"] = flow.target_label(db, target) if flow.target_label else target
    row = _jobs(db, CHECK_KIND, key).order_by(Job.id.desc()).first()
    ai = None if target else last_ai(db, name)
    if row is None:
        return {**base, "status": "none", "result": None, "ai": ai}
    done = row if row.status == "done" else _jobs(db, CHECK_KIND, key).filter(Job.status == "done").order_by(Job.id.desc()).first()
    return {**base, "status": row.status, "job": row.id, "error": row.error or "",
            "result": (done.payload or {}).get("result") if done else None,
            "queued_at": row.created_at.isoformat() if row.created_at else None, "ai": ai}


def queue_check(db: Session, name: str, *, target: str | None = None, auto: bool = False, actor: str = "") -> Any:
    from app.jobs import enqueue

    key = job_key(name, target)
    return enqueue(db, CHECK_KIND, key, object_type="flow", payload={"auto": auto, "actor": actor})


def previous_result(db: Session, name: str, before_job: int | None = None) -> dict[str, Any] | None:
    from app.models import Job

    q = _jobs(db, CHECK_KIND, name).filter(Job.status == "done")
    if before_job:
        q = q.filter(Job.id < before_job)
    row = q.order_by(Job.id.desc()).first()
    return (row.payload or {}).get("result") if row is not None else None


def _broken(result: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {f"{lane['id']}:{s['id']}": s for lane in (result or {}).get("lanes", []) for s in lane["steps"] if s["status"] == "bad"}


def notice_changes(db: Session, name: str, before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """Journal a block that broke (an error: the Settings badge) and one that works again; queue a
    ForgeAI analysis of the new break when ForgeAI is on. Returns the newly broken labels."""
    from app.jobs import enqueue
    from app.journal import report

    if "@" in name:  # a per-object check, run on request: the page shows it, nothing is journaled
        return []
    flow = get(name)
    old, new = _broken(before), _broken(after)
    broke = [new[k] for k in new if k not in old]
    healed = [old[k] for k in old if k not in new]
    for s in broke:
        report(db, name, "flow", "error", summary=f"{flow.label} flow broke at {s['label']}: {s['detail']}"[:300],
               detail=f"{s.get('fix', '')}\nSettings › Integrations › {flow.label} › Flow and log", object_type="integration", object_id=name)
    for s in healed:
        report(db, name, "flow", "ok", summary=f"{flow.label} flow works again at {s['label']}", object_type="integration", object_id=name)
    if broke and ai_on():
        question = ("These blocks just broke: " + "; ".join(f"{s['label']} ({s['detail']})" for s in broke)
                    + ". Where does the flow break, why, and what should the operator do first?")
        enqueue(db, AI_KIND, name, object_type="flow", payload={"question": question[:900]})
    db.commit()
    return [s["label"] for s in broke]


def run_ai(db: Session, name: str, question: str) -> dict[str, Any]:
    """Job handler (LLM lane): ForgeAI's analysis of a new break, kept as a logged call."""
    from app import forgeai

    call = forgeai.run(db, "flow", None, actor="system", question=question, flow=name)
    return {"call_id": call.id, "status": call.status, "reason": call.reason or ""}


def watch_due(db: Session, name: str, now: datetime | None = None) -> bool:
    """Due when watching is on, the integration is set up and its last check is older than WATCH_EVERY."""
    from app.models import Job

    if not watch_on() or not get(name).configured():
        return False
    row = _jobs(db, CHECK_KIND, name).order_by(Job.id.desc()).first()
    if row is None or row.created_at is None:
        return True
    at = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - at).total_seconds() >= WATCH_EVERY


def queue_due(db: Session) -> list[str]:
    """Escalation loop step: queue every set-up flow whose check is due. Returns their names."""
    names = [name for name in flows() if watch_due(db, name)]
    for name in names:
        queue_check(db, name, auto=True)
    if names:
        db.commit()
    return names


# --- Logs and the ForgeAI context ---------------------------------------------------------------

def container_log_lines(name: str, limit: int = 60) -> dict[str, Any]:
    """The last log lines that belong to this flow: Core lines with its words, plus its own services
    (docker-ro, 5 s timeout, masked). ``error`` says why there are none."""
    import httpx

    from app.containers import logs

    flow = get(name)
    lines: list[str] = []
    errors: list[str] = []
    for service in flow.services:
        try:
            out = logs(service, 1000 if service == "core" else 200)
        except LookupError as exc:
            errors.append(str(exc))
            continue
        except httpx.HTTPError as exc:
            return {"lines": [], "error": f"docker-ro does not answer ({exc.__class__.__name__}): docker compose up -d docker-ro"}
        rows = out.get("lines") or []
        if service == "core":
            rows = [line for line in rows if any(w in line.lower() for w in flow.log_words)] if flow.log_words else []
        else:  # the flow's own services: only warnings and errors
            rows = [line for line in rows if any(w in line.lower() for w in ("level=error", "level=warn", "error", "failed"))]
        lines += [f"{service} | {line}" for line in rows[-limit:]]
    return {"lines": lines[-limit:], "error": "; ".join(errors) if errors and not lines else ""}


def flow_context(db: Session, name: str) -> dict[str, Any]:
    """What ForgeAI reads in a Flow chat: the last check, the recent calls and the log lines, short."""
    from app.exchanges import list_exchanges

    flow = get(name)
    result = last_flow(db, name).get("result") or {}
    blocks = [{"lane": lane["id"], "block": s["label"], "status": s["status"], "detail": s["detail"], "fix": s.get("fix", ""),
               **({"details": s["rows"][:8]} if s.get("rows") else {})}
              for lane in result.get("lanes", []) for s in lane["steps"]]
    exchanges = [{k: r[k] for k in ("at", "direction", "kind", "peer", "status", "http_status", "ms", "summary")}
                 for r in list_exchanges(db, name, limit=40)["rows"]]
    logs = container_log_lines(name, 30)
    out = {"integration": flow.label, "flow_checked_at": result.get("checked_at"), "flow_blocks": blocks, "recent_exchanges": exchanges,
           "log_lines": [line[-300:] for line in logs["lines"]], "ports": {"forgesre_http": forgesre_port()}}
    if flow.extra_context is not None:
        out.update(flow.extra_context(db))
    return out
