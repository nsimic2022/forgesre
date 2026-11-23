"""ForgeAI investigator: one model, a few read-only tools, and an answer that must cite its evidence.

Not a chat of many agents: a local model needs 20-60 s per call, and every extra agent invents
more. The investigator starts from numbered evidence (ForgeRCA facts, changes before the alarm,
similar confirmed incidents, the playbook), may ask for at most ``MAX_STEPS`` tool calls within
``BUDGET_S`` seconds, then answers in a fixed shape:

    {"cause": ..., "confidence": "low|medium|high", "evidence": ["E2", "E5"], "next_steps": [...]}

Code checks every cited evidence id exists and the usual ForgeAI rules (no invented IPs or
numbers, no shell commands) against all evidence it was shown. An answer that fails is kept as
rejected, never shown as the incident's answer. Tools only read, each with a short timeout:
metric (Prometheus, the asset's own queries), logs (Loki), interfaces (LibreNMS ports),
similar (our confirmed incidents) and asset (the asset card and its parent).

The whole exchange is one ``ForgeAiCall`` (task ``investigate``) with every prompt and answer.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.orm import Session

from app.models import ForgeAiCall, Incident

TASK = "investigate"
MAX_STEPS = 6
BUDGET_S = 240.0
TOOL_TIMEOUT = 4.0
CONFIDENCE = ("low", "medium", "high")

PROTOCOL = (
    "You investigate one incident with numbered evidence. Each turn answer with JSON only, one of:\n"
    '  {"tool": "<name>", "args": {...}, "why": "what you expect to learn"}\n'
    '  {"answer": {"cause": "one or two sentences", "confidence": "low|medium|high", '
    '"evidence": ["E1", "E3"], "next_steps": ["check 1", "check 2"]}}\n'
    "Cite in evidence the ids that support the cause; every claim must come from cited evidence. "
    "If the evidence does not show the cause, say so with confidence low. Ask for a tool only when it can change the answer.\n"
    "Tools:\n"
    '  metric {"name": one of the metric names listed} - last hours of that metric on this asset\n'
    '  logs {"contains": "word"} - up to 15 log lines of this host around the alarm (Loki)\n'
    "  interfaces {} - interfaces that are down on this device (LibreNMS)\n"
    "  similar {} - our resolved incidents most like this one, with the confirmed cause\n"
    "  asset {} - the asset, its parent and whether the parent has an open incident\n"
    '  docs {"query": "words"} - excerpts of uploaded vendor manuals and runbooks\n'
)


class Evidence:
    """Numbered evidence the model may cite. Ids are E1, E2, ... in the order they were added."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, source: str, text: str) -> str:
        eid = f"E{len(self.items) + 1}"
        self.items.append({"id": eid, "source": source, "text": " ".join(str(text).split())[:400]})
        return eid

    def ids(self) -> set[str]:
        return {i["id"] for i in self.items}

    def text(self) -> str:
        return "\n".join(f"{i['id']} {i['text']}" for i in self.items)


def _asset_dict(asset) -> dict[str, Any]:
    from app.dc_noc import _asset_query_dict

    return _asset_query_dict(asset)


def initial_evidence(db: Session, incident: Incident) -> Evidence:
    """E1… before any tool: the alarm, ForgeRCA facts and guess, changes before, the playbook, similar confirmed incidents."""
    from app.changes import changes_before
    from app.forgeai import _when
    from app.knowledge import playbooks_for, similar_confirmed
    from app.models import Investigation

    ev = Evidence()
    asset = incident.asset
    ev.add("incident", f"Alarm '{incident.title}' severity {incident.severity}, status {incident.status}, started {_when(incident.started_at)}"
           + (f" on {asset.hostname} ({asset.type}, {asset.environment})" if asset else ""))
    rca = (db.query(Investigation).filter(Investigation.incident_id == incident.id, ~Investigation.provider.like("%-llm"))
           .order_by(Investigation.id.desc()).first())
    if rca is not None:
        for fact in (rca.result or {}).get("facts") or []:
            if isinstance(fact, dict) and fact.get("text"):
                ev.add("forgerca", fact["text"])
        if rca.likely_cause:
            ev.add("forgerca", f"ForgeRCA's rule-based guess: {rca.likely_cause}")
    for change in changes_before(db, incident)[:5]:
        ev.add("change", f"{change.get('kind') or 'change'} {change.get('before_min')} min before: {change.get('title')}")
    for pb in playbooks_for(db, incident, limit=1):
        ev.add("playbook", f"Playbook {pb['name']}: " + "; ".join(pb["steps"]))
    for s in similar_confirmed(db, incident, limit=3):
        ev.add("similar", f"{s['incident']} {s['title']} on {s['host'] or '?'} ({s['why']}): confirmed cause: {s['confirmed_cause']}")
    from app.known_errors import known_errors_for

    for k in known_errors_for(db, incident)[:2]:
        ev.add("known_error", f"Known error ({k['status']}) '{k['title']}': " + "; ".join(
            f"{label}: {k[key]}" for label, key in (("shows as", "symptoms"), ("workaround", "workaround"), ("fix", "fix")) if k[key]))
    return ev


def metric_names(incident: Incident) -> list[str]:
    from rca.collector import promql_queries_for

    if incident.asset is None:
        return []
    return sorted(k for k, (expr, _unit) in promql_queries_for(_asset_dict(incident.asset)).items() if expr)


def run_tool(db: Session, incident: Incident, name: str, args: dict[str, Any]) -> tuple[str, str]:
    """(source, text) for one tool call. Never raises; a failure is evidence too ("not available")."""
    asset = incident.asset
    try:
        if name == "metric":
            from rca.collector import promql_queries_for

            from app.asset_metrics import incident_graph_bounds
            from app.services import query_prometheus_range

            key = str(args.get("name") or "")
            packed = promql_queries_for(_asset_dict(asset)) if asset is not None else {}
            if key not in packed:
                return "metric", f"metric {key!r} is not available; names: {', '.join(metric_names(incident)) or 'none'}"
            expr, unit = packed[key]
            bounds = incident_graph_bounds(incident)
            got = query_prometheus_range(expr, start=bounds["start"], end=bounds["end"], step="5m", timeout=TOOL_TIMEOUT)
            values = [float(v) for v in got.get("values") or [] if v is not None]
            if not values:
                return "metric", f"{key}: no samples in Prometheus for this asset around the alarm"
            return "metric", (f"{key} ({unit or 'value'}) around the alarm: first {values[0]:.1f}, min {min(values):.1f}, "
                              f"max {max(values):.1f}, last {values[-1]:.1f}, {len(values)} samples")
        if name == "logs":
            from app.asset_metrics import incident_graph_bounds
            from app.services import query_loki

            if asset is None:
                return "logs", "no asset, so no host logs"
            bounds = incident_graph_bounds(incident)
            word = str(args.get("contains") or "").strip()[:40]
            lines = query_loki(limit=40, query=asset.hostname or asset.ip, start=bounds["start"], end=bounds["end"])
            if word:
                lines = [ln for ln in lines if word.lower() in ln.lower()]
            return "logs", ("log lines: " + " | ".join(ln[:160] for ln in lines[:15])) if lines else f"no log lines{' with ' + word if word else ''}"
        if name == "interfaces":
            from app import librenms

            if asset is None or not librenms.configured():
                return "interfaces", "LibreNMS is not set up for this asset"
            ports = librenms.run_sync(librenms.get_ports(asset.hostname or asset.ip))
            down = [p.get("ifName") or p.get("ifDescr") for p in ports if str(p.get("ifOperStatus")) == "down" and str(p.get("ifAdminStatus")) == "up"]
            return "interfaces", f"{len(ports)} interfaces; down while enabled: {', '.join(map(str, down[:12])) or 'none'}"
        if name == "similar":
            from app.knowledge import similar_confirmed

            rows = similar_confirmed(db, incident, limit=5)
            return "similar", "; ".join(f"{s['incident']} ({s['why']}): {s['confirmed_cause']}" for s in rows) or "no similar incident with a confirmed cause"
        if name == "asset":
            from app.services import ACTIVE_INCIDENT_STATUSES

            if asset is None:
                return "asset", "the incident has no asset"
            parent = asset.parent
            open_parent = parent is not None and db.query(Incident).filter(
                Incident.asset_id == parent.id, Incident.status.in_(ACTIVE_INCIDENT_STATUSES)).count() > 0
            return "asset", (f"{asset.hostname} type {asset.type}, owner {asset.owner or 'unknown'}; parent "
                             + (f"{parent.hostname} ({'has an open incident' if open_parent else 'no open incident'})" if parent else "none recorded"))
        if name == "docs":
            from app.doc_search import for_prompt

            query = str(args.get("query") or incident.title or "")[:200]
            hits = for_prompt(db, query, k=3)
            return "docs", " | ".join(f"{h['doc']} p.{h['page']}: {h['text'][:300]}" for h in hits) or "no vendor document matches"
    except Exception as exc:  # a broken source is evidence too, not a crash
        return name, f"{name} not available: {str(exc)[:160]}"
    return "tool", f"unknown tool {name!r}"


def _check(parsed: dict[str, Any] | None, ev: Evidence) -> tuple[dict[str, Any], str]:
    from app.forgeai import IP, SHELL

    from rca.llm import invented_numbers

    if not isinstance(parsed, dict):
        return {}, "the model did not return JSON"
    cause = " ".join(str(parsed.get("cause") or "").split())[:400]
    if not cause:
        return {}, "the answer has no cause"
    confidence = str(parsed.get("confidence") or "").lower().strip()
    if confidence not in CONFIDENCE:
        return {}, "confidence must be low, medium or high"
    cited = [str(e).strip().upper() for e in (parsed.get("evidence") or []) if str(e).strip()]
    if not cited:
        return {}, "the answer cites no evidence"
    missing = [e for e in cited if e not in ev.ids()]
    if missing:
        return {}, f"the answer cites evidence that does not exist: {', '.join(missing[:4])}"
    steps = parsed.get("next_steps") or []
    steps = [steps] if isinstance(steps, str) else steps
    steps = [" ".join(str(s).split())[:200] for s in steps if str(s).strip()][:4]
    text = " ".join([cause, *steps])
    known = ev.text()
    if set(IP.findall(text)) - set(IP.findall(known)):
        return {}, "the answer names an IP address that is not in the evidence"
    if invented_numbers(text, known):
        return {}, "the answer has a number that is not in the evidence"
    if SHELL.search(text):
        return {}, "the answer contains a shell command"
    return {"cause": cause, "confidence": confidence, "evidence": sorted(set(cited), key=lambda e: int(e[1:])), "next_steps": steps}, ""


def investigate(db: Session, incident: Incident, *, actor: str = "system", skill_slug: str = "", trial: bool = False,
                instructions: str | None = None) -> ForgeAiCall:
    """Run the loop once and record it. ``trial`` keeps the answer out of the incident (replays, skill tests)."""
    from app import forgeai
    from app.journal import report
    from app.settings import settings

    skill = forgeai.pick_skill(db, TASK, incident, skill_slug)
    if skill is None:
        raise LookupError("no ForgeAI skill for investigations")
    text = instructions if instructions is not None else skill.instructions
    system = forgeai.BASE_RULES.format(shape=forgeai.TASKS[TASK]["shape"]) + "\n" + PROTOCOL + "\nInstructions for this kind of work:\n" + (text or "").strip()
    ev = initial_evidence(db, incident)
    call = ForgeAiCall(incident_id=incident.id, task=TASK, skill_slug=skill.slug, skill_version=skill.version or 0,
                       system_prompt=system, actor=actor, endpoint=(settings.llm_url or "") if settings.ai_enabled else "")
    provider = forgeai._provider()
    transcript: list[str] = []
    raws: list[str] = []
    started = time.monotonic()
    names = metric_names(incident)
    if provider.get_name() == "none":
        call.status, call.reason = "error", "ForgeAI is off (Settings › RCA / AI)"
    else:
        call.model = provider.get_model() or ""
        final: dict[str, Any] | None = None
        for step in range(MAX_STEPS + 1):
            left = MAX_STEPS - step
            user = json.dumps({"incident": incident.number.split("_")[0], "evidence": ev.items, "metric_names": names,
                               "tool_calls_left": left,
                               "note": "Answer now." if left == 0 or time.monotonic() - started > BUDGET_S else ""},
                              ensure_ascii=False, default=str)
            transcript.append(user)
            parsed = provider.complete_json(system, user, 500)
            raws.append(str(getattr(provider, "last_raw", "") or ""))
            if parsed is None:
                call.status, call.reason = "error", str(getattr(provider, "last_error", "") or "no answer")
                break
            if isinstance(parsed.get("answer"), dict) or "cause" in parsed:
                final = parsed.get("answer") if isinstance(parsed.get("answer"), dict) else parsed
                break
            tool = str(parsed.get("tool") or "")
            if left <= 0 or time.monotonic() - started > BUDGET_S or not tool:
                call.status, call.reason = "rejected", "no answer within the tool calls and time allowed"
                break
            source, result = run_tool(db, incident, tool, parsed.get("args") if isinstance(parsed.get("args"), dict) else {})
            eid = ev.add(source, result)
            transcript.append(f"tool {tool} {json.dumps(parsed.get('args') or {})} -> {eid}")
        if final is not None:
            answer, why = _check(final, ev)
            call.answer = answer or final
            call.status, call.reason = ("rejected", why) if why else ("trial" if trial else "used", "")
    call.duration_ms = int((time.monotonic() - started) * 1000)
    call.user_prompt = "\n\n".join(transcript)[:60000]
    call.raw_response = "\n\n".join(raws)[:60000]
    call.answer = {**(call.answer or {}), "_evidence": ev.items}
    db.add(call)
    db.commit()
    report(db, "forgeai", TASK, "ok" if call.status in {"used", "trial"} else "error",
           summary=f"{incident.number.split('_')[0]} investigation with {skill.slug}: {call.status}"
                   f"{' (' + call.reason + ')' if call.reason else ''}, {len(ev.items)} evidence, {call.duration_ms} ms",
           object_type="incident", object_id=incident.number)
    return call


def latest(db: Session, incident: Incident) -> dict[str, Any] | None:
    """The newest non-trial investigation of this incident for the incident page, evidence resolved to text."""
    call = (db.query(ForgeAiCall).filter(ForgeAiCall.incident_id == incident.id, ForgeAiCall.task == TASK,
                                         ForgeAiCall.status != "trial").order_by(ForgeAiCall.id.desc()).first())
    if call is None:
        return None
    answer = dict(call.answer or {})
    evidence = {e["id"]: e for e in answer.pop("_evidence", []) if isinstance(e, dict)}
    return {"call_id": call.id, "status": call.status, "reason": call.reason, "at": call.created_at.isoformat() if call.created_at else None,
            "model": call.model, "seconds": round((call.duration_ms or 0) / 1000), "answer": answer,
            "cited": [evidence[e] for e in answer.get("evidence") or [] if e in evidence], "evidence_count": len(evidence)}

