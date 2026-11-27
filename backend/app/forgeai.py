"""ForgeAI: put one narrow question to the language model, check the answer, keep the exchange.

How it works, for the next developer:

* A **task** says what is asked and the exact JSON shape of the answer (incident: cause, first
  checks, a line for the shift lead; explain; postmortem; shift summary). Tasks are code.
* A **skill** (``ForgeAiSkill`` row) is the editable part: when it applies (alert names, asset
  types) and the instructions, written like a note to a colleague. The fixed safety rules and the
  JSON shape always wrap it, so an edited skill cannot switch them off.
* The **context** is built from the database only: the incident, the asset, ForgeRCA facts and
  hypotheses, the playbook, earlier incidents with a confirmed cause, changes before; syslog when
  the skill asks for it. Secrets are stripped (rca.sanitize) before the prompt is built.
* The answer is **checked**: JSON shape, lengths, no number or IP that is not in the context, no
  shell commands. A failed check keeps the answer out of the UI and says why.
* Every exchange is a ``ForgeAiCall`` row: exact prompts, raw answer, model, time, outcome.

Runs only in the jobs worker (LLM lane); the model call has the configured timeout.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import ForgeAiCall, ForgeAiSkill, Incident, Investigation, utcnow

JOB_KIND = "forgeai"
QUESTION_MAX = 500
# ~1 200 tokens: Qwen3-4B on CPU reads about 25 tokens/s before it writes (docs/llm.md)
CONTEXT_MAX = 4500

BASE_RULES = (
    "You are ForgeAI, the assistant inside ForgeSRE, a NOC appliance. You only read; you never act.\n"
    "Rules you must follow:\n"
    "- Use only the facts in the user message. If something is not there, say it is unknown.\n"
    "- Never invent numbers, host names, IP addresses or log lines.\n"
    "- Never give shell commands and never tell anyone to restart, delete or change something; "
    "say what to check instead.\n"
    "- Plain English, short sentences, for a NOC operator at 3 am.\n"
    "- vendor_docs (D1, D2…) are excerpts of manuals; use them only when they fit these facts and "
    "cite them like [D1].\n"
    "- Answer with JSON only, exactly this shape: {shape}\n"
)

# Chat without an incident: general NOC and IT questions. The model may use what it knows, but it
# still only reads and never hands out commands that change a system.
# Chat on an integration's Flow page (Settings › Integrations › Zabbix › Flow): only this path.
FLOW_RULES = (
    "You are ForgeAI, the assistant inside ForgeSRE, a NOC appliance. You only read; you never act.\n"
    "This chat is about the connection between ForgeSRE and {integration} (integration in the context).\n"
    "Rules you must follow:\n"
    "- flow_blocks is the last check of every step in both directions (status ok, warn, bad, unknown); "
    "recent_exchanges are the last calls between the two systems, newest first; zabbix_send_attempts are "
    "what Zabbix itself tried to send.\n"
    "- Find where the path breaks and why, from these facts. Name the block. Say plainly when the facts "
    "do not show it, and what to look at to find out.\n"
    "- You may use general knowledge about Zabbix, webhooks, HTTP, TLS, DNS and firewalls. Never invent "
    "addresses, versions or results that are not in the facts.\n"
    "- Steps to fix are read-only checks or settings a person changes; never commands that delete or restart "
    "something on their own. Never ask for or repeat a token or password.\n"
    "- Answer the operator_question; earlier_in_this_chat is the conversation so far. Short, for a NOC operator.\n"
    "- Answer with JSON only, exactly this shape: {shape}\n"
)

CHAT_RULES = (
    "You are ForgeAI, the assistant inside ForgeSRE, a NOC appliance. You only read; you never act.\n"
    "Rules you must follow:\n"
    "- Answer the operator_question; earlier_in_this_chat is the conversation so far.\n"
    "- You may use general knowledge about networks, servers, databases and monitoring. Say when you "
    "are not sure, and never invent facts about this site's hosts, addresses or incidents.\n"
    "- Never give commands that restart, delete or change something; read-only checks are fine.\n"
    "- Plain English, short paragraphs or a short list, for a NOC operator.\n"
    "- vendor_docs (D1, D2…) are excerpts of manuals; use them when they fit and cite them like [D1].\n"
    "- Answer with JSON only, exactly this shape: {shape}\n"
)

# Chat about an incident, by how freely the model may answer (ai.forgeai.chat_mode, or per chat).
# The safety rules (only read, no commands that change a system, no invented site facts) never move.
INCIDENT_CHAT_BALANCED = (
    "You are ForgeAI, the assistant inside ForgeSRE, a NOC appliance. You only read; you never act.\n"
    "Rules you must follow:\n"
    "- Answer the operator_question; earlier_in_this_chat is the conversation so far.\n"
    "- For questions about this incident, its hosts, metrics and logs, use only the facts in the user "
    "message; if something is not there, say it is unknown.\n"
    "- Other questions (general IT, networking, how something works, simple arithmetic, anything not "
    "about this incident) answer from general knowledge, briefly, and say it is general knowledge.\n"
    "- Never invent numbers, host names, IP addresses or log lines of this site.\n"
    "- Never give commands that restart, delete or change something; read-only checks are fine.\n"
    "- Plain English, short paragraphs or a short list, for a NOC operator.\n"
    "- vendor_docs (D1, D2…) are excerpts of manuals; use them when they fit and cite them like [D1].\n"
    "- Answer with JSON only, exactly this shape: {shape}; even a one-line reply goes inside it.\n"
)
INCIDENT_CHAT_OPEN = (
    "You are ForgeAI, the assistant inside ForgeSRE, a NOC appliance. You only read; you never act.\n"
    "Rules you must follow:\n"
    "- Answer the operator_question freely, using the incident facts in the user message and your general "
    "knowledge; earlier_in_this_chat is the conversation so far.\n"
    "- Mark what comes from the facts and what is your general knowledge or a guess.\n"
    "- Never invent numbers, host names, IP addresses or log lines of this site.\n"
    "- Never give commands that restart, delete or change something; read-only checks are fine.\n"
    "- vendor_docs (D1, D2…) are excerpts of manuals; use them when they fit and cite them like [D1].\n"
    "- Answer with JSON only, exactly this shape: {shape}; even a one-line reply goes inside it.\n"
)
CHAT_MODES = ("strict", "balanced", "open")


def chat_rules(incident: Incident | None, mode: str = "") -> str:
    """System rules for a chat turn: without an incident the general CHAT_RULES; with one, by mode.

    strict = facts of the incident only (BASE_RULES), balanced = incident facts for incident questions
    and general knowledge for the rest, open = both freely, marked. Empty mode = ai.forgeai.chat_mode.
    """
    if incident is None:
        return CHAT_RULES
    from app.settings import settings

    mode = mode if mode in CHAT_MODES else settings.forgeai_chat_mode
    return {"strict": BASE_RULES, "open": INCIDENT_CHAT_OPEN}.get(mode, INCIDENT_CHAT_BALANCED)


def plain_answer(raw: str) -> str:
    """The reply text from a model answer that is not valid JSON: thinking and code fences removed;
    a half-written {"answer": "…"} gives its text. Empty when there is nothing to show."""
    text = re.sub(r"(?s)<think>.*?(</think>|$)", "", raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    m = re.match(r'(?s)^\{\s*"answer"\s*:\s*"(.*?)("\s*\}?\s*)?$', text)
    if m:
        text = m.group(1).replace("\\n", "\n").replace('\\"', '"')
    return text[:3000].strip()


THINK_BUDGET = 1024  # default extra tokens when ai.llm.thinking is on (ai.llm.think_tokens)

# ai.forgeai.language: the language of the text ForgeAI writes (JSON keys and values the checks
# compare stay as they are). English is the default; models answer best in it.
LANGUAGES = {"en": "English", "sr": "Serbian"}
LANGUAGE_RULES = {
    "sr": ("- Write every text value in Serbian, Latin script, ekavian (proveri, not provjeri). Keep JSON keys, "
           "the fixed words low/medium/high and agree/partly/disagree, host names, metric names, evidence ids "
           "and quoted log lines exactly as they are.\n"),
}


def language() -> str:
    """Answer language from settings (ai.forgeai.language): en or sr."""
    from app.settings import settings

    value = str((((settings.yaml or {}).get("ai") or {}).get("forgeai") or {}).get("language") or "en").strip().lower()
    return value if value in LANGUAGES else "en"


def with_language(system: str) -> str:
    """The system prompt with the answer-language rule added (nothing changes for English)."""
    rule = LANGUAGE_RULES.get(language())
    if not rule:
        return system
    return system.replace("- Plain English,", "- Plain words,") + rule

TASKS: dict[str, dict[str, Any]] = {
    "incident": {
        "label": "Cause and first checks",
        "shape": '{"cause": "one sentence", "first_checks": ["check 1", "check 2", "check 3"], "for_shift_lead": "one sentence"}',
        "fields": {"cause": 300, "for_shift_lead": 300},
        "lists": {"first_checks": (3, 200)},
        "max_tokens": 400,
    },
    "explain": {
        "label": "Explain the alarm",
        "shape": '{"explanation": "2-4 sentences", "why_it_matters": "one sentence"}',
        "fields": {"explanation": 700, "why_it_matters": 300},
        "lists": {},
        "max_tokens": 350,
    },
    "investigate": {
        "label": "Investigation with tools",
        "shape": '{"cause": "one or two sentences", "confidence": "low|medium|high", "evidence": ["E1"], "next_steps": ["check"]}',
        "fields": {"cause": 400},
        "lists": {"next_steps": (4, 200)},
        "max_tokens": 500,
    },
    "postmortem": {
        "label": "Incident review summary",
        "shape": '{"paragraph": "one paragraph, 4-7 sentences"}',
        "fields": {"paragraph": 1400},
        "lists": {},
        "max_tokens": 500,
    },
    "ask": {
        "label": "Your question",
        "shape": '{"answer": "2-6 sentences", "not_in_facts": ["what the facts do not show"]}',
        "fields": {"answer": 1500},
        "lists": {"not_in_facts": (4, 200)},
        "max_tokens": 500,
    },
    "chat": {
        "label": "Chat",
        "shape": '{"answer": "your reply; use \\n for new lines"}',
        "fields": {"answer": 3000},
        "lists": {},
        "max_tokens": 800,
    },
    "flow": {
        "label": "Integration flow",
        "shape": '{"answer": "your reply; use \\n for new lines"}',
        "fields": {"answer": 3000},
        "lists": {},
        "max_tokens": 800,
    },
    "critic": {
        "label": "Second opinion",
        "shape": '{"verdict": "agree|partly|disagree", "why": "1-3 sentences", "problems": ["claim not supported by the facts"], "better_cause": "only if you disagree"}',
        "fields": {"verdict": 12, "why": 600},
        "lists": {"problems": (4, 240)},
        "max_tokens": 500,
    },
    "storm": {
        "label": "Alarm storm summary",
        "shape": '{"summary": "2-4 sentences", "common_cause": "one sentence, or unknown", "first_checks": ["check 1", "check 2", "check 3"]}',
        "fields": {"summary": 900, "common_cause": 300},
        "lists": {"first_checks": (3, 200)},
        "max_tokens": 450,
    },
    "shift": {
        "label": "Shift summary",
        "shape": '{"summary": "3-5 sentences", "still_open": ["item"], "watch": ["item"]}',
        "fields": {"summary": 1200},
        "lists": {"still_open": (8, 200), "watch": (5, 200)},
        "max_tokens": 500,
    },
}

INCLUDE_DEFAULT = {"facts": True, "playbook": True, "similar": True, "changes": True, "syslog": False, "docs": True}

# Built-in skills: slug, name, task, alert patterns, asset types, priority, include overrides, instructions.
BUILTIN: list[tuple[str, str, str, list[str], list[str], int, dict[str, bool], str]] = [
    ("flow-where-it-breaks", "Where the flow breaks", "flow", [], [], 1000, {"docs": False},
     "Go through the blocks in the order a message travels. The first block that is bad or unknown is "
     "usually the place; explain what it means in one or two sentences, then give the fix from its fix "
     "field in your own words and how to confirm it worked (which block should turn green). If Zabbix's "
     "send attempts show an error, quote the error and translate it."),
    ("incident-generic", "Any incident", "incident", [], [], 1000, {},
     "Read the facts and the hypotheses. Pick the most likely cause the facts support and say how sure "
     "you are in plain words. First checks go from quickest and safest to slower ones. If an earlier "
     "incident on the same asset had a confirmed cause, say whether this one looks the same."),
    ("linux-disk", "Linux disk full", "incident", ["*disk*", "*filesystem*", "*inode*", "*/u0*", "*/var*"], ["Linux Server", "Oracle DB", "PostgreSQL"], 100, {},
     "A filesystem is filling. Name the mount point from the facts. Typical causes: logs or archive logs "
     "not rotated, a backup or dump written locally, a runaway temp file. First checks: what grew in the "
     "last hours, whether log rotation and archive purge jobs ran, whether a backup wrote to this disk."),
    ("cpu-memory", "CPU or memory high", "incident", ["*cpu*", "*memory*", "*load*", "*swap*", "*oom*"], [], 200, {},
     "Resource pressure. Say whether it is a spike or a steady climb from the facts. Look for a deploy or "
     "config change just before, a batch job at that hour, or the same pattern in earlier incidents."),
    ("host-down", "Host or service down", "incident", ["*down*", "*unreachable*", "*instancedown*", "*no data*"], [], 150, {"syslog": True},
     "The host or its exporter stopped answering. Separate 'the machine is down' from 'only the exporter "
     "or the path to it is down': use ping, the exporter port and the parent device in the facts. If the "
     "parent switch or hypervisor also has an alarm, say that it is probably the real cause."),
    ("network-link", "Network link or interface", "incident", ["*interface*", "*link*", "*port*", "*bgp*", "*ospf*", "*if*down*"], ["Switch", "Router", "Firewall", "Network device"], 90, {"syslog": True},
     "A network interface or neighbour went down. Use the syslog lines (LINK-DOWN, err-disable, flapping) "
     "if present. Name the interface from the facts. First checks: the far end, the cable or optic, "
     "errors on the port, and any change made to the device before the alarm."),
    ("windows-service", "Windows service", "incident", ["*service*", "*windows*"], ["Windows Server"], 120, {},
     "A Windows service or server problem. Say which service if the facts name it. First checks: the "
     "service state and its last restart, Windows updates or a reboot before the alarm, disk and memory."),
    ("database", "Database", "incident", ["*oracle*", "*postgres*", "*mysql*", "*sql*", "*db*", "*tablespace*", "*replication*"], ["Oracle DB", "PostgreSQL", "MySQL", "MSSQL"], 110, {},
     "A database problem. Say which part (space, sessions, replication, availability) the facts point to. "
     "First checks: space and archive logs, long-running sessions or locks, replication lag, and recent "
     "deploys of applications that use this database."),
    ("kubernetes", "Kubernetes workload", "incident", ["*pod*", "*crashloop*", "*kube*", "*node*notready*"], ["Kubernetes Cluster"], 80, {},
     "A Kubernetes node or workload problem. Say whether it is one pod, one deployment or a node. First "
     "checks: recent rollouts, restarts and the reason Kubernetes gives, and the node's state."),
    ("appliance-checks", "Certificate, backup or heartbeat", "incident", ["*certificate*", "*tls*", "*backup*", "*heartbeat*"], [], 70, {},
     "One of ForgeSRE's own checks. Say plainly what expires or went silent and by when it matters. "
     "First checks: who owns the certificate, job or script, and when it last succeeded."),
    ("investigate-generic", "Deeper investigation", "investigate", [], [], 1000, {},
     "Find the most likely cause. Start from the ForgeRCA facts and the changes before the alarm. Use a "
     "tool only to confirm or rule out one idea: the metric that should have moved, log lines with the "
     "error word, interfaces on a network device, similar incidents. Prefer a low-confidence answer that "
     "cites real evidence over a confident guess."),
    ("explain-generic", "Explain for Tier 1", "explain", [], [], 1000, {"similar": False, "changes": False},
     "Explain to a new Tier 1 operator what this alarm means on this kind of asset, without jargon. Say "
     "why it matters for users and how urgent it is from the severity. Do not guess the cause."),
    ("postmortem-generic", "Incident review summary", "postmortem", [], [], 1000, {"syslog": False},
     "Write the summary paragraph of a blameless incident review: what happened, when, impact, how it was "
     "found and fixed, and the confirmed cause if there is one. Use the notes and the timeline. Mark "
     "anything not in the facts as unknown."),
    ("ask-generic", "Operator question", "ask", [], [], 1000, {},
     "An operator asks one question about this incident; it is in operator_question. Answer only that "
     "question from the facts, in plain words. If the facts cannot answer it, say so and list what is "
     "missing in not_in_facts. Do not repeat the whole incident."),
    ("chat-generic", "Chat", "chat", [], [], 1000, {},
     "Talk with a NOC operator. When the chat is about an incident, answer from its facts first and say "
     "what the facts do not show. Keep answers short; offer the next useful check instead of a lecture."),
    ("storm-generic", "Alarm storm", "storm", [], [], 1000, {},
     "Many alarms opened at once and are linked to this storm. From the incidents and the counts by "
     "site, rack, type and parent device, say what they have in common and the most likely shared "
     "cause (power, an uplink or core switch, storage, a hypervisor, a change). Name the device or "
     "place only if the counts show it. First checks: the shared thing first, then how to confirm it."),
    ("shift-generic", "Shift handover", "shift", [], [], 1000, {},
     "Summarise the last hours for the next shift: what happened, what is still open and who has it, "
     "and what to keep an eye on. Most important first. Do not repeat every incident."),
]



def answer_tokens(task: str) -> int:
    """max_tokens for one task: the built-in length times ai.llm.answer_percent."""
    from app.settings import settings

    return max(64, TASKS[task]["max_tokens"] * settings.llm_answer_percent // 100)


def answer_lengths() -> list[dict[str, Any]]:
    """Every task with its built-in and current answer length, for Settings › RCA / AI."""
    return [{"task": name, "label": spec.get("label") or name, "base": spec["max_tokens"], "now": answer_tokens(name)} for name, spec in TASKS.items()]

def seed_skills(db: Session) -> None:
    """Add missing built-in skills; edited ones keep their text, defaults are refreshed."""
    existing = {s.slug: s for s in db.query(ForgeAiSkill).all()}
    for slug, name, task, alerts, types, prio, include, text in BUILTIN:
        row = existing.get(slug)
        if row is None:
            db.add(ForgeAiSkill(slug=slug, name=name, task=task, match_alerts=alerts, match_types=types, priority=prio,
                                instructions=text, default_instructions=text, include={**INCLUDE_DEFAULT, **include}, builtin=True))
        else:
            row.default_instructions = text
    db.flush()


def _matches(patterns: list[str], value: str) -> bool:
    value = (value or "").lower()
    return any(fnmatch.fnmatch(value, str(p).lower()) or str(p).lower().strip("*") in value for p in patterns if str(p).strip())


def pick_skill(db: Session, task: str, incident: Incident | None, slug: str = "") -> ForgeAiSkill | None:
    """The named skill, else the enabled skill for ``task`` whose alert and type patterns match, lowest priority first."""
    seed_skills(db)
    if slug:
        return db.query(ForgeAiSkill).filter_by(slug=slug).first()
    rows = db.query(ForgeAiSkill).filter_by(task=task, enabled=True).order_by(ForgeAiSkill.priority, ForgeAiSkill.id).all()
    title = (incident.title if incident else "") or ""
    # fingerprint is "alertname:asset" for Prometheus alarms
    alert = (incident.fingerprint or "").split(":", 1)[0] if incident is not None else ""
    atype = incident.asset.type if incident is not None and incident.asset is not None else ""
    for row in rows:
        alert_ok = not row.match_alerts or _matches(row.match_alerts, title) or _matches(row.match_alerts, alert)
        type_ok = not row.match_types or atype in row.match_types
        if (row.match_alerts or row.match_types) and alert_ok and type_ok:
            return row
    return next((r for r in rows if not r.match_alerts and not r.match_types), rows[0] if rows else None)


INC_LONG = re.compile(r"\b(INC-\d+)_[\d.]+_[\d:]+")


def _short(text: Any) -> str:
    """INC-0003_09.11.2026_12:01 -> INC-0003, so the model repeats the short number."""
    return INC_LONG.sub(r"\1", str(text or ""))


def _when(value: Any) -> str:
    """A time as people read it: appliance time zone, minutes only ("09.11. 12:01")."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from app.settings import settings

    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value[:16]
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(settings.timezone or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%d.%m. %H:%M")


def _incident_context(db: Session, incident: Incident, include: dict[str, Any]) -> dict[str, Any]:
    from app.changes import changes_before
    from app.incident_view import similar_before

    asset = incident.asset
    ctx: dict[str, Any] = {
        "incident": {"number": incident.number.split("_")[0], "title": incident.title, "severity": incident.severity,
                     "status": incident.status, "started": _when(incident.started_at),
                     "acknowledged_by": incident.ack_by or "", "resolved": _when(incident.resolved_at)},
        "asset": {"hostname": asset.hostname, "ip": asset.ip, "type": asset.type, "environment": asset.environment,
                  "owner": asset.owner} if asset else None,
    }
    rca = (db.query(Investigation).filter(Investigation.incident_id == incident.id, ~Investigation.provider.like("%-llm"))
           .order_by(Investigation.id.desc()).first())
    if include.get("facts", True) and rca is not None:
        result = rca.result or {}
        ctx["forgerca"] = {
            "likely_cause": rca.likely_cause, "confidence_percent": int(rca.confidence or 0),
            "facts": [f.get("text") for f in result.get("facts") or [] if isinstance(f, dict)][:12],
            "hypotheses": [h.get("summary") for h in result.get("hypotheses") or [] if isinstance(h, dict)][:4],
        }
    if include.get("playbook", True) and incident.playbook is not None:
        ctx["playbook"] = [str(s.get("title") if isinstance(s, dict) else s) for s in incident.playbook.steps or []][:10]
    if include.get("similar", True):
        ctx["earlier_on_this_asset"] = [{"started": _when(s["started_at"]), "confirmed_cause": s["root_cause"], "minutes": s["minutes"]}
                                        for s in similar_before(db, incident) if s["root_cause"]]
    if include.get("changes", True):
        ctx["changes_before"] = [{"title": c.get("title"), "kind": c.get("kind"), "minutes_before": c.get("before_min")}
                                 for c in changes_before(db, incident)[:6]]
    if include.get("notes", False) or include.get("postmortem", False):
        from app.history import note_as_dict, notes_for

        ctx["notes"] = [n["body"] for n in (note_as_dict(r) for r in notes_for(db, incident))][:15]
        ctx["confirmed_cause"] = incident.root_cause or ""
        ctx["timeline"] = [f"{_when(i.get('at'))} {_short(i.get('title'))}" for i in (incident.timeline or [])][:20]
    if include.get("syslog", False):
        from app.dc_noc import incident_syslog_lines

        ctx["syslog"] = (incident_syslog_lines(incident) or [])[:20]
    return ctx


def _shift_context(db: Session, hours: int = 12) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=hours)
    rows = (db.query(Incident).options(joinedload(Incident.asset))
            .filter((Incident.started_at >= since) | Incident.status.in_(("OPEN", "ACK", "INVESTIGATING", "ESCALATED")))
            .order_by(Incident.started_at.desc()).limit(40).all())
    return {"hours": hours, "incidents": [
        {"number": i.number.split("_")[0], "title": i.title, "severity": i.severity, "status": i.status,
         "host": i.asset.hostname if i.asset else "", "started": _when(i.started_at),
         "acknowledged_by": i.ack_by or "", "confirmed_cause": i.root_cause or ""} for i in rows]}


def _storm_context(db: Session, storm: Incident) -> dict[str, Any]:
    """The incidents linked to a storm plus counts by site, rack, type and parent device."""
    from collections import Counter

    from app.asset_extras import normalize_extras

    rows = (db.query(Incident).options(joinedload(Incident.asset)).filter(Incident.caused_by_id == storm.id)
            .order_by(Incident.started_at).limit(200).all())
    counts: dict[str, Counter] = {k: Counter() for k in ("site", "rack", "type", "parent", "alert")}
    shown = []
    for row in rows:
        asset = row.asset
        extras = normalize_extras(asset.extras) if asset else {}
        parent = asset.parent.hostname if asset is not None and asset.parent is not None else ""
        for key, value in (("site", extras.get("site")), ("rack", extras.get("rack")), ("type", asset.type if asset else ""),
                           ("parent", parent), ("alert", (row.fingerprint or "").split(":", 1)[0])):
            if value:
                counts[key][value] += 1
        if len(shown) < 25:
            shown.append({"title": row.title, "host": asset.hostname if asset else "", "type": asset.type if asset else "",
                          "site": extras.get("site", ""), "rack": extras.get("rack", ""), "parent": parent, "started": _when(row.started_at)})
    return {
        "storm": {"number": storm.number.split("_")[0], "started": _when(storm.started_at), "linked_incidents": len(rows)},
        "most_common": {k: [{"value": v, "incidents": n} for v, n in c.most_common(4)] for k, c in counts.items() if c},
        "incidents": shown,
    }


def queue_storm_summary(db: Session, storm: Incident, *, opened: bool = False) -> None:
    """Ask ForgeAI to summarise a storm: when it opens and again every 25 linked incidents.

    Runs inside alert intake, so it only adds the job row (no commit; intake commits). Only with
    the local model on; a pending summary for the same storm is not added twice.
    """
    from app.models import Job
    from app.settings import settings

    if not settings.ai_enabled:
        return
    linked = db.query(Incident).filter(Incident.caused_by_id == storm.id).count()
    if not opened and (linked == 0 or linked % 25):
        return
    object_id = f"{storm.number}#storm"
    if db.query(Job).filter(Job.kind == "forgeai", Job.object_id == object_id, Job.status.in_(["pending", "running"])).first():
        return
    db.add(Job(kind="forgeai", status="pending", object_type="incident", object_id=object_id,
               payload={"task": "storm", "actor": "system"}))


def build_prompts(db: Session, task: str, skill: ForgeAiSkill, incident: Incident | None, *, hours: int = 12,
                  question: str = "", history: list[dict[str, str]] | None = None, mode: str = "", flow: str = "") -> tuple[str, str]:
    """(system, user) exactly as sent: fixed rules + task shape + skill instructions, and the context JSON.

    ``question`` (tasks ask and chat) goes first in the context as operator_question and is never trimmed
    away. ``history`` (chat) is the last turns of the conversation; a chat without an incident gets
    CHAT_RULES, which allow general knowledge.
    """
    from rca.sanitize import sanitize

    spec = TASKS[task]
    include = {**INCLUDE_DEFAULT, **(skill.include or {})}
    if task == "postmortem":
        include["postmortem"] = True
    if task == "flow":
        from app.zabbix_flow import flow_context

        context = flow_context(db, flow or "zabbix")
    elif task == "shift":
        context = _shift_context(db, hours)
    elif task == "storm" and incident is not None:
        context = _storm_context(db, incident)
    elif incident is None:
        context = {}
    else:
        context = _incident_context(db, incident, include)
    if task == "flow":
        rules = FLOW_RULES.replace("{integration}", (flow or "zabbix").capitalize())
    else:
        rules = chat_rules(incident, mode) if task == "chat" else BASE_RULES
    system = with_language(rules.format(shape=spec["shape"])) + "\nInstructions for this kind of work:\n" + (skill.instructions or "").strip()
    if task in {"ask", "chat", "flow"}:
        context = {"operator_question": " ".join(question.split())[:QUESTION_MAX], **context}
    if task in {"chat", "flow"} and history:
        context["earlier_in_this_chat"] = [{"who": h.get("role", ""), "text": " ".join(str(h.get("text", "")).split())[:600]} for h in history]
    if task not in {"shift", "storm"} and (incident is not None or task == "chat") and include.get("docs", True):
        from app.doc_search import for_prompt

        asset_type = incident.asset.type if incident is not None and incident.asset is not None else ""
        docs = for_prompt(db, " ".join(filter(None, [question, incident.title if incident is not None else "", asset_type])))
        if docs:
            context["vendor_docs"] = docs
    context = json.loads(_short(json.dumps(sanitize(context), ensure_ascii=False, default=str)))
    user = json.dumps(context, ensure_ascii=False, default=str)
    # Too long: drop the least useful parts first, so the JSON stays whole instead of being cut.
    for key in ("syslog", "timeline", "vendor_docs", "changes_before", "earlier_on_this_asset", "playbook", "notes", "earlier_in_this_chat"):
        if len(user) <= CONTEXT_MAX:
            break
        context.pop(key, None)
        user = json.dumps(context, ensure_ascii=False, default=str)
    while len(user) > CONTEXT_MAX and len(context.get("incidents") or []) > 1:
        context["incidents"].pop()  # shift summary: the oldest go first
        context["more_incidents_not_shown"] = context.get("more_incidents_not_shown", 0) + 1
        user = json.dumps(context, ensure_ascii=False, default=str)
    return system, user[:CONTEXT_MAX]


IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SHELL = re.compile(r"(^|[\s`'\"])(sudo|rm\s+-|systemctl\s|service\s+\S+\s+(restart|stop)|kill\s|reboot|shutdown|mkfs|dd\s+if=|docker\s|kubectl\s+delete)", re.I)


def check_answer(task: str, parsed: dict[str, Any] | None, context: str, *, strict: bool = True,
                 numbers: bool | None = None) -> tuple[dict[str, Any], str]:
    """(clean answer, reason) — reason is empty when the answer may be shown.

    ``strict=False`` (a general chat with no incident) skips the IP and invented-number checks, since
    there are no facts to hold numbers against; the shell-command check always runs.
    """
    from rca.llm import invented_numbers

    if not isinstance(parsed, dict):
        return {}, "the model did not return JSON"
    spec = TASKS[task]
    answer: dict[str, Any] = {}
    for key, limit in spec["fields"].items():
        raw = str(parsed.get(key) or "")
        # chat answers keep their line breaks (lists, paragraphs); other fields are one line
        value = "\n".join(" ".join(line.split()) for line in raw.strip().splitlines()).strip() if task == "chat" else " ".join(raw.split())
        if not value:
            return {}, f"the answer has no {key}"
        answer[key] = value[:limit]
    for key, (count, limit) in spec["lists"].items():
        items = parsed.get(key) or []
        items = [items] if isinstance(items, str) else items
        answer[key] = [" ".join(str(i).split())[:limit] for i in items if str(i).strip()][:count]
    text = " ".join([*(v for v in answer.values() if isinstance(v, str)), *(i for v in answer.values() if isinstance(v, list) for i in v)])
    if strict and set(IP.findall(text)) - set(IP.findall(context)):
        return {}, "the answer names an IP address that is not in the facts"
    # numbers: None = like strict; a balanced or open chat about an incident may use general-knowledge numbers
    if (strict if numbers is None else numbers) and invented_numbers(text, context):
        return {}, "the answer has a number that is not in the facts"
    if SHELL.search(text):
        return {}, "the answer contains a shell command"
    return answer, ""


def _provider():
    from rca.llm import make_provider

    from app.settings import settings

    return make_provider(settings.llm_url if settings.ai_enabled else None, settings.llm_model, timeout=settings.llm_timeout,
                         thinking=settings.llm_thinking)


def run(db: Session, task: str, incident: Incident | None = None, *, skill_slug: str = "", actor: str = "system",
        trial: bool = False, instructions: str | None = None, hours: int = 12, question: str = "",
        cloud: bool = False, history: list[dict[str, str]] | None = None, on_progress: Any = None, mode: str = "",
        flow: str = "") -> ForgeAiCall:
    """Ask the model once and record the exchange. ``trial`` (skill test) never becomes the incident's answer;
    ``instructions`` tries unsaved skill text; ``hours`` is the shift summary window. An incident answer that passes the checks is stored as a
    ``forgerca-llm`` Investigation so the incident page and reports show it. ``on_progress(phase, thinking,
    answer)`` streams a local answer so the chat can show that the model is working."""
    from app.journal import report
    from app.settings import settings

    if task not in TASKS:
        raise ValueError(f"unknown task {task}")
    if task == "ask":
        if incident is None or not question.strip():
            raise ValueError("a question needs an incident and some text")
        trial = True  # an operator's question is never the incident's answer by itself
    if task == "chat":
        if not question.strip():
            raise ValueError("a chat message needs some text")
        trial = True
    if task == "flow":
        question = question.strip() or "Where does the flow break, and what should I do?"
        trial = True
    if task == "investigate":
        # several steps with read-only tools; see forgeai_investigator
        from app.forgeai_investigator import investigate

        if incident is None:
            raise ValueError("an investigation needs an incident")
        return investigate(db, incident, actor=actor, skill_slug=skill_slug, trial=trial, instructions=instructions)
    skill = pick_skill(db, task, incident, skill_slug)
    if skill is None:
        raise LookupError("no ForgeAI skill for this task")
    if instructions is not None:
        # unsaved text from the skill editor: a transient copy, never added to the session
        skill = ForgeAiSkill(slug=skill.slug, name=skill.name, task=skill.task, include=skill.include, version=skill.version, instructions=instructions)
    system, user = build_prompts(db, task, skill, incident, hours=hours, question=question, history=history, mode=mode, flow=flow)
    call = ForgeAiCall(incident_id=incident.id if incident is not None else None, task=task, skill_slug=skill.slug,
                       skill_version=skill.version or 0, system_prompt=system, user_prompt=user, actor=actor,
                       endpoint=(settings.llm_url or "") if settings.ai_enabled else "")
    if cloud:
        # An Engineer chose the paid cloud model for this one question (app.cloud_llm); the prompt
        # above is exactly what leaves the server.
        from app.cloud_llm import provider as cloud_provider

        provider = cloud_provider()
        call.endpoint = f"cloud:{settings.cloud_provider} {settings.cloud_url}"
    else:
        provider = _provider()
    if provider.get_name() == "none":
        call.status, call.reason = "error", "ForgeAI is off (Settings › RCA / AI)"
    else:
        started = time.monotonic()
        # nothing open while the model works (can take minutes): progress writes from other sessions go through
        db.commit()
        # a thinking model needs room to think before the JSON answer
        budget = answer_tokens(task) + (settings.llm_think_tokens if settings.llm_thinking and not cloud else 0)
        if on_progress is not None and hasattr(provider, "complete_json_stream"):
            parsed = provider.complete_json_stream(system, user, budget, on_progress)
        else:
            parsed = provider.complete_json(system, user, budget)
        call.duration_ms = int(getattr(provider, "last_ms", 0) or (time.monotonic() - started) * 1000)
        call.raw_response = str(getattr(provider, "last_raw", "") or "")
        thought = str(getattr(provider, "last_thinking", "") or "")
        if thought:
            # kept with the raw answer so Investigations shows how the model got there
            call.raw_response = f"<think>\n{thought}\n</think>\n{call.raw_response}"
        call.model = provider.get_model() or ""
        if parsed is None and task in {"chat", "flow"}:
            # a chat reply is free text anyway: a model that forgot the JSON wrapper still answered
            text = plain_answer(str(getattr(provider, "last_raw", "") or ""))
            parsed = {"answer": text} if text else None
        if parsed is None:
            call.status, call.reason = "error", str(getattr(provider, "last_error", "") or "no answer")
        else:
            # chat about an incident: invented IP addresses are always refused; numbers only in strict mode
            strict = not (task == "chat" and incident is None)
            # flow: ports, times and counts are everywhere in the facts; invented IPs are still refused
            numbers = strict and task != "flow" and (task != "chat" or chat_rules(incident, mode) is BASE_RULES)
            answer, why = check_answer(task, parsed, user, strict=strict, numbers=numbers)
            call.answer = answer or parsed
            call.status, call.reason = ("rejected", why) if why else ("trial" if trial else "used", "")
    db.add(call)
    db.flush()
    if call.status == "used" and task == "incident" and incident is not None:
        store_incident_answer(db, incident, call, skill, actor)
    if call.status == "used" and task == "storm" and incident is not None:
        from app.services import append_timeline

        # unique node id: append_timeline replaces an entry with the same id
        append_timeline(incident, f"forgeai-storm-{call.id}", "FORGEAI",
                        f"ForgeAI storm summary: {call.answer.get('summary', '')} Likely shared cause: {call.answer.get('common_cause', 'unknown')}")
    db.commit()
    report(db, "forgeai", task, "ok" if call.status in {"used", "trial"} else "error",
           summary=f"{(incident.number.split('_')[0] + ' ') if incident is not None else ''}{task} with {skill.slug}: {call.status}"
                   f"{' (' + call.reason + ')' if call.reason else ''} in {call.duration_ms} ms",
           object_type="incident" if incident is not None else "forgeai", object_id=incident.number if incident is not None else task)
    return call


CRITIC_SYSTEM = (
    "You review another model's answer about one data-center incident. You get the same facts it got "
    "and its answer under answer_under_review. Judge only against the facts: is the cause supported, "
    "are the first checks sensible and safe, is anything invented? Do not add new facts.\n"
    "- verdict: agree (supported and useful), partly (some claims unsupported or checks weak), "
    "disagree (the cause does not follow from the facts).\n"
    "- problems: each unsupported or wrong claim, short. better_cause: only when you disagree, from the facts.\n"
    "- Answer with JSON only, exactly this shape: {shape}"
)


def critique(db: Session, call: ForgeAiCall, *, actor: str) -> ForgeAiCall:
    """Second opinion: the paid cloud model checks a local "cause and first checks" answer.

    Only an Engineer starts it (one call at a time, audited by the API); it sends the reviewed call's
    own prompt (already sanitised) plus its answer. The result is a trial ForgeAiCall of task
    "critic" on the same incident; ``answer.reviews_call`` points at the reviewed call.
    """
    from app.cloud_llm import provider as cloud_provider
    from app.settings import settings

    if call.task != "incident" or not call.answer or call.status not in {"used", "trial"}:
        raise ValueError("only a checked 'cause and first checks' answer can get a second opinion")
    spec = TASKS["critic"]
    system = with_language(CRITIC_SYSTEM.replace("{shape}", spec["shape"]) + "\n")
    reviewed = {k: call.answer.get(k) for k in ("cause", "first_checks", "for_shift_lead")}
    user = (call.user_prompt or "")[:CONTEXT_MAX] + "\n" + json.dumps({"answer_under_review": reviewed}, ensure_ascii=False)
    row = ForgeAiCall(incident_id=call.incident_id, task="critic", skill_slug=call.skill_slug, skill_version=call.skill_version,
                      system_prompt=system, user_prompt=user, actor=actor, endpoint=f"cloud:{settings.cloud_provider} {settings.cloud_url}")
    model = cloud_provider()
    started = time.monotonic()
    parsed = model.complete_json(system, user, answer_tokens("critic"))
    row.duration_ms = int(getattr(model, "last_ms", 0) or (time.monotonic() - started) * 1000)
    row.raw_response = str(getattr(model, "last_raw", "") or "")
    row.model = model.get_model() or ""
    if parsed is None:
        row.status, row.reason = "error", str(getattr(model, "last_error", "") or "no answer")
    else:
        answer, why = check_answer("critic", parsed, user)
        verdict = str((answer or {}).get("verdict", "")).lower()
        if not why and verdict not in {"agree", "partly", "disagree"}:
            why = "the verdict is not agree, partly or disagree"
        if answer:
            answer["verdict"] = verdict
            better = " ".join(str(parsed.get("better_cause") or "").split())[:400]
            if better and verdict == "disagree":
                answer["better_cause"] = better
            answer["reviews_call"] = call.id
        row.answer = answer or parsed
        row.status, row.reason = ("rejected", why) if why else ("trial", "")
    db.add(row)
    db.commit()
    return row


def store_incident_answer(db: Session, incident: Incident, call: ForgeAiCall, skill: ForgeAiSkill | None, actor: str) -> Investigation:
    """Make a checked "cause and first checks" answer the incident's ForgeAI answer (an Investigation row)."""
    a = call.answer
    row = Investigation(
        incident_id=incident.id, summary=a["for_shift_lead"], likely_cause=a["cause"],
        # provider name kept from V0.x: reports and the incident view read "forgerca-llm"
        recommended_action="\n".join(a.get("first_checks") or []), provider="forgerca-llm", engine="forgeai",
        engine_version="2.0", model=call.model, requested_by=actor, confidence=0,
        result={"answer": a, "call_id": call.id, "skill": call.skill_slug, "skill_name": skill.name if skill else call.skill_slug},
        disclaimer="ForgeAI wrote this from ForgeRCA facts; it has not changed anything.",
    )
    db.add(row)
    return row


def use_trial_answer(db: Session, call: ForgeAiCall, actor: str) -> Investigation:
    """An Engineer picks a trial "cause and first checks" answer (another skill) as the incident's answer.

    Only a trial that passed the checks qualifies; the call becomes "used" and the choice is audited.
    """
    from app.audit import audit

    if call.task != "incident" or call.status != "trial" or call.incident_id is None:
        raise ValueError("only a checked trial answer of 'cause and first checks' can be used")
    incident = db.get(Incident, call.incident_id)
    skill = db.query(ForgeAiSkill).filter_by(slug=call.skill_slug).first()
    row = store_incident_answer(db, incident, call, skill, actor)
    call.status, call.reason = "used", f"chosen by {actor}"
    audit(db, "forgeai.use", actor=actor, object_type="incident", object_id=incident.number, data={"call_id": call.id, "skill": call.skill_slug})
    db.commit()
    return row


def call_as_dict(call: ForgeAiCall, *, prompts: bool) -> dict[str, Any]:
    """One exchange for the UI; ``prompts`` adds the exact prompts and raw answer (Analyst and up)."""
    out = {"id": call.id, "at": call.created_at.isoformat() if call.created_at else None, "task": call.task,
           "skill": call.skill_slug, "skill_version": call.skill_version, "model": call.model, "status": call.status,
           "reason": call.reason, "duration_ms": call.duration_ms, "answer": call.answer or {}, "rating": call.rating, "stars": call.stars or 0, "actor": call.actor,
           "cloud": (call.endpoint or "").startswith("cloud:")}
    if prompts:
        out.update(system_prompt=call.system_prompt, user_prompt=call.user_prompt, raw_response=call.raw_response)
    return out


def status(db: Session) -> dict[str, Any]:
    """Settings › ForgeAI: is it on, does llama.cpp answer and with which model, and how the last calls went."""
    import httpx

    from app.settings import settings

    out: dict[str, Any] = {"enabled": settings.ai_enabled, "url": settings.llm_url or "", "model": settings.llm_model,
                           "timeout_s": settings.llm_timeout, "reachable": None, "loaded_model": "", "why": ""}
    if settings.ai_enabled and settings.llm_url:
        try:
            r = httpx.get(settings.llm_url.rstrip("/") + "/models", timeout=3.0)
            r.raise_for_status()
            rows = r.json().get("data") or []
            out["reachable"], out["loaded_model"] = True, str(rows[0].get("id")) if rows else ""
        except Exception as exc:  # status page: never raise
            out["reachable"], out["why"] = False, str(exc)[:200]
    since = utcnow() - timedelta(days=7)
    recent = db.query(ForgeAiCall).filter(ForgeAiCall.created_at >= since).all()
    done = [c.duration_ms for c in recent if c.status in {"used", "trial", "rejected"} and c.duration_ms]
    out["week"] = {
        "calls": len(recent), "used": sum(c.status == "used" for c in recent), "rejected": sum(c.status == "rejected" for c in recent),
        "errors": sum(c.status == "error" for c in recent), "avg_ms": int(sum(done) / len(done)) if done else 0,
        "thumbs_up": sum(c.rating > 0 for c in recent), "thumbs_down": sum(c.rating < 0 for c in recent),
        **_stars([c.stars or 0 for c in recent]),
    }
    last = db.query(ForgeAiCall).order_by(ForgeAiCall.id.desc()).limit(10).all()
    out["last"] = [call_as_dict(c, prompts=False) for c in last]
    return out


PING_KIND = "forgeai_ping"


def ping(db: Session) -> dict[str, Any]:
    """Job handler: one tiny question to the local model, timed, so people can see it really answers.

    Returns ok, total and first-token milliseconds, the model and the error; stored in the job payload
    and shown by GET /forgeai/pulse. Never raises.
    """
    import time

    from app.settings import settings

    provider = _provider()
    if provider.get_name() == "none":
        return {"ok": False, "error": "ForgeAI is off (Settings › RCA / AI)", "ms": 0}
    started = time.monotonic()
    first: dict[str, int] = {}

    def seen(phase: str, thinking: str, answer: str) -> None:
        if phase != "waiting" and "ms" not in first:
            first["ms"] = int((time.monotonic() - started) * 1000)

    system, user = "Answer with JSON only.", 'Reply exactly {"ok": true}'
    if hasattr(provider, "complete_json_stream"):
        parsed = provider.complete_json_stream(system, user, 24 + (THINK_BUDGET if settings.llm_thinking else 0), seen)
    else:
        parsed = provider.complete_json(system, user, 24)
    ms = int(getattr(provider, "last_ms", 0) or (time.monotonic() - started) * 1000)
    ok = bool(parsed) and parsed.get("ok") is True
    return {"ok": ok, "ms": ms, "first_ms": first.get("ms", 0), "model": provider.get_model() or "",
            "error": "" if ok else (str(getattr(provider, "last_error", "") or "") or "unexpected answer")[:200],
            "raw": str(getattr(provider, "last_raw", "") or "")[:200]}


def pulse(db: Session, *, check: bool = True) -> dict[str, Any]:
    """One small status line for the chat: local model reachable and loaded, the last test, the LLM
    lane and its worker, the cloud model. ``check`` asks llama.cpp /models (2 s timeout)."""
    import httpx

    from app.cloud_llm import info
    from app.jobs import llm_lane
    from app.loop_beats import age_seconds
    from app.models import Job
    from app.settings import settings

    on = bool(settings.ai_enabled and settings.llm_url)
    local: dict[str, Any] = {"on": on, "model": settings.llm_model, "reachable": None, "why": ""}
    if on and check:
        try:
            r = httpx.get(settings.llm_url.rstrip("/") + "/models", timeout=2.0)
            r.raise_for_status()
            rows = r.json().get("data") or []
            local["reachable"] = True
            if rows and rows[0].get("id"):
                local["model"] = str(rows[0]["id"])
        except Exception as exc:  # status line: never raise
            local["reachable"], local["why"] = False, str(exc)[:160]
    last = db.query(Job).filter(Job.kind == PING_KIND).order_by(Job.id.desc()).first()
    test: dict[str, Any] = {"status": "none"}
    if last is not None:
        result = (last.payload or {}).get("result") or {}
        if last.status in {"pending", "running"}:
            test = {"status": last.status}
        elif last.status == "done":
            test = {"status": "ok" if result.get("ok") else "error", **{k: result.get(k) for k in ("ms", "first_ms", "model", "error")}}
        else:
            test = {"status": "error", "error": (last.error or "")[:200]}
        test["at"] = (last.finished_at or last.created_at).isoformat() if (last.finished_at or last.created_at) else None
    worker_age = age_seconds("llm-jobs")
    lane = llm_lane(db)
    lane.pop("pending_ids", None)
    return {"local": local, "test": test, "lane": lane, "worker_age_s": int(worker_age) if worker_age is not None else None,
            "timeout_s": int(settings.llm_timeout), "thinking": settings.llm_thinking, "cloud": info()}


EDITABLE = ("name", "match_alerts", "match_types", "priority", "instructions", "include", "enabled")


def skill_as_dict(skill: ForgeAiSkill, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "slug": skill.slug, "name": skill.name, "task": skill.task, "task_label": TASKS.get(skill.task, {}).get("label", skill.task),
        "match_alerts": list(skill.match_alerts or []), "match_types": list(skill.match_types or []), "priority": skill.priority,
        "instructions": skill.instructions, "default_instructions": skill.default_instructions if skill.builtin else "",
        "include": {**INCLUDE_DEFAULT, **(skill.include or {})}, "enabled": skill.enabled, "builtin": skill.builtin,
        "version": skill.version, "updated_by": skill.updated_by, "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
        "history": list(skill.history or [])[-20:], "stats": stats or {},
    }


def _stars(values: list[int]) -> dict[str, Any]:
    """Average of the 1–5 star ratings given (unrated answers left out) and how many there were."""
    rated = [v for v in values if v > 0]
    return {"stars_avg": round(sum(rated) / len(rated), 1) if rated else None, "stars_n": len(rated)}


def skill_stats(db: Session, days: int = 30) -> dict[str, dict[str, Any]]:
    """Per skill over ``days``: questions, used, rejected, errors, thumbs up and down, stars, average seconds."""
    since = utcnow() - timedelta(days=days)
    out: dict[str, dict[str, Any]] = {}
    for c in db.query(ForgeAiCall).filter(ForgeAiCall.created_at >= since, ForgeAiCall.status != "trial").all():
        s = out.setdefault(c.skill_slug, {"calls": 0, "used": 0, "rejected": 0, "errors": 0, "up": 0, "down": 0, "ms": 0, "_stars": []})
        s["calls"] += 1
        s["used"] += c.status == "used"
        s["rejected"] += c.status == "rejected"
        s["errors"] += c.status == "error"
        s["up"] += c.rating > 0
        s["down"] += c.rating < 0
        s["ms"] += c.duration_ms or 0
        s["_stars"].append(c.stars or 0)
    for s in out.values():
        s["avg_s"] = round(s.pop("ms") / max(1, s["calls"]) / 1000, 1)
        s.update(_stars(s.pop("_stars")))
    return out


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "name" in data:
        out["name"] = " ".join(str(data["name"]).split())[:128] or "Skill"
    for key in ("match_alerts", "match_types"):
        if key in data:
            items = data[key] if isinstance(data[key], list) else str(data[key]).split(",")
            out[key] = [str(i).strip()[:80] for i in items if str(i).strip()][:20]
    if "priority" in data:
        out["priority"] = max(1, min(10000, int(data["priority"])))
    if "instructions" in data:
        text = str(data["instructions"]).strip()
        if len(text) < 20:
            raise ValueError("instructions need at least a sentence")
        out["instructions"] = text[:3000]
    if "include" in data and isinstance(data["include"], dict):
        out["include"] = {k: bool(data["include"].get(k)) for k in INCLUDE_DEFAULT}
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    return out


def save_skill(db: Session, slug: str, data: dict[str, Any], actor: str) -> ForgeAiSkill:
    """Update a skill; the old version goes to ``history`` (last 20) and the audit log."""
    seed_skills(db)
    skill = db.query(ForgeAiSkill).filter_by(slug=slug).first()
    if skill is None:
        raise LookupError("skill not found")
    changes = _clean(data)
    if not changes:
        return skill
    if skill.task in {"explain", "postmortem", "shift"} and (changes.get("match_alerts") or changes.get("match_types")):
        changes.pop("match_alerts", None)
        changes.pop("match_types", None)
    skill.history = [*(skill.history or []), {
        "version": skill.version, "instructions": skill.instructions, "match_alerts": skill.match_alerts, "match_types": skill.match_types,
        "include": skill.include, "by": skill.updated_by, "at": skill.updated_at.isoformat() if skill.updated_at else None,
    }][-20:]
    for key, value in changes.items():
        setattr(skill, key, value)
    skill.version = (skill.version or 1) + 1
    skill.updated_by, skill.updated_at = actor, utcnow()
    return skill


def create_skill(db: Session, data: dict[str, Any], actor: str) -> ForgeAiSkill:
    """A new incident skill. Its slug comes from the name; built-ins keep theirs."""
    seed_skills(db)
    clean = _clean({**data, "instructions": data.get("instructions", "")})
    base = re.sub(r"[^a-z0-9]+", "-", clean.get("name", "skill").lower()).strip("-")[:48] or "skill"
    slug, n = base, 2
    while db.query(ForgeAiSkill).filter_by(slug=slug).first() is not None:
        slug, n = f"{base}-{n}", n + 1
    task = str(data.get("task") or "incident")
    if task not in TASKS:
        raise ValueError("unknown task")
    skill = ForgeAiSkill(slug=slug, task=task, builtin=False, default_instructions="", updated_by=actor,
                         include={**INCLUDE_DEFAULT, **clean.pop("include", {})}, **{"priority": 50, **clean})
    db.add(skill)
    db.flush()
    return skill
