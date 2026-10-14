"""Dashboard ForgeAI facts. SQL first; the LLM only rewrites short sentences.

Never dumps the incident table into the model. Demo rows stay out of the digest.
Not on the alarm path.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.journal import report
from app.models import Asset, Incident, JournalEntry, utcnow
from app.services import compact_when_text, is_demo_incident

REPEAT_DAYS = 4
REPEAT_MIN = 3
AGE_HOURS = 4
MONTH_DAYS = 30
WEEK_DAYS = 7
ACTIVE = ("OPEN", "INVESTIGATING", "ESCALATED")
OPEN_AGING = ("OPEN", "ESCALATED")
REWRITE_MAX = 4000


def _alertname(row: Incident) -> str:
    payload = row.alert_payload if isinstance(row.alert_payload, dict) else {}
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if isinstance(labels, dict) and labels.get("alertname"):
        return str(labels.get("alertname") or "").strip()
    return ""


def _extra(asset: Asset | None, key: str) -> str:
    extras = asset.extras if asset is not None and isinstance(asset.extras, dict) else {}
    return " ".join(str(extras.get(key) or "").split())


def _host(row: Incident) -> str:
    asset = row.asset
    if asset is not None:
        return asset.hostname or asset.asset_id or "—"
    return "—"


def _problem_key(row: Incident) -> str:
    name = _alertname(row) or (row.title or "").strip() or "Incident"
    return name.strip().lower()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _real_incidents(db: Session, since: datetime | None = None) -> list[Incident]:
    query = db.query(Incident).options(joinedload(Incident.asset)).order_by(Incident.started_at.desc(), Incident.id.desc())
    if since is not None:
        query = query.filter(Incident.started_at >= since)
    return [row for row in query.all() if not is_demo_incident(row)]


def insight_facts(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Bounded counts. Safe to run on every Dashboard GET."""
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    month_start = moment - timedelta(days=MONTH_DAYS)
    week_start = moment - timedelta(days=WEEK_DAYS)
    prior_week = week_start - timedelta(days=WEEK_DAYS)
    repeat_start = moment - timedelta(days=REPEAT_DAYS)
    age_cut = moment - timedelta(hours=AGE_HOURS)

    month = _real_incidents(db, month_start)
    older = _real_incidents(db, moment - timedelta(days=MONTH_DAYS * 2))
    all_active = [
        row
        for row in db.query(Incident).options(joinedload(Incident.asset)).filter(Incident.status.in_(ACTIVE)).all()
        if not is_demo_incident(row)
    ]

    repeats: list[dict[str, Any]] = []
    buckets: dict[tuple[str, str], list[Incident]] = defaultdict(list)
    for row in month:
        started = _aware(row.started_at)
        if started is None or started < repeat_start:
            continue
        asset = row.asset
        asset_key = asset.asset_id if asset is not None else ""
        if not asset_key:
            continue
        buckets[(asset_key, _problem_key(row))].append(row)
    for (asset_key, problem), items in buckets.items():
        if len(items) < REPEAT_MIN:
            continue
        latest = items[0]
        repeats.append(
            {
                "asset": asset_key,
                "hostname": _host(latest),
                "alertname": _alertname(latest) or latest.title or problem,
                "count": len(items),
                "days": REPEAT_DAYS,
                "last": latest.number,
            }
        )
    repeats.sort(key=lambda item: (-int(item["count"]), item["hostname"]))

    def group_label(row: Incident) -> tuple[str, str]:
        asset = row.asset
        if asset is None:
            return ("", "")
        extras = (
            ("rack", _extra(asset, "rack")),
            ("row", _extra(asset, "row")),
            ("site", _extra(asset, "site")),
        )
        for kind, value in extras:
            if value:
                return (kind, value)
        parent = asset.parent
        if parent is not None:
            return ("parent", parent.hostname or parent.asset_id)
        return ("", "")

    def week_counts(start: datetime, end: datetime) -> Counter[tuple[str, str]]:
        counts: Counter[tuple[str, str]] = Counter()
        for row in month:
            started = _aware(row.started_at)
            if started is None or started < start or started >= end:
                continue
            label = group_label(row)
            if label[1]:
                counts[label] += 1
        return counts

    this_week = week_counts(week_start, moment)
    last_week = week_counts(prior_week, week_start)
    hot: list[dict[str, Any]] = []
    for label, count in this_week.items():
        prev = last_week.get(label, 0)
        if count >= prev + 2 or (prev == 0 and count >= 3):
            kind, name = label
            hot.append({"kind": kind, "name": name, "this_week": count, "last_week": prev})
    hot.sort(key=lambda item: (-int(item["this_week"]), item["name"]))

    parent_active = {row.asset.id for row in all_active if row.asset is not None}
    children = (
        db.query(Incident)
        .options(joinedload(Incident.asset).joinedload(Asset.parent))
        .filter(Incident.status.in_(ACTIVE))
        .all()
    )
    parent_child: list[dict[str, Any]] = []
    seen_pc: set[str] = set()
    for row in children:
        if is_demo_incident(row):
            continue
        asset = row.asset
        parent = asset.parent if asset is not None else None
        if asset is None or parent is None or parent.id not in parent_active:
            continue
        key = f"{parent.asset_id}:{asset.asset_id}:{_problem_key(row)}"
        if key in seen_pc:
            continue
        seen_pc.add(key)
        parent_child.append(
            {
                "parent": parent.hostname or parent.asset_id,
                "child": asset.hostname or asset.asset_id,
                "incident": row.title or row.number,
                "number": row.number,
            }
        )

    aging: list[dict[str, Any]] = []
    for row in all_active:
        if row.status not in OPEN_AGING or row.ack_at:
            continue
        started = _aware(row.started_at)
        if started is None or started > age_cut:
            continue
        hours = int((moment - started).total_seconds() // 3600)
        aging.append(
            {
                "hostname": _host(row),
                "incident": row.title or row.number,
                "number": row.number,
                "status": row.status,
                "hours": hours,
            }
        )
    aging.sort(key=lambda item: (-int(item["hours"]), item["hostname"]))

    severity = Counter((row.severity or "").upper() or "UNKNOWN" for row in month)
    hosts = Counter(_host(row) for row in month if _host(row) != "—")
    seen_before = {
        (_host(row), _problem_key(row))
        for row in older
        if _aware(row.started_at) is not None and _aware(row.started_at) < month_start
    }
    repeat_month = 0
    new_month = 0
    month_keys: set[tuple[str, str]] = set()
    for row in month:
        key = (_host(row), _problem_key(row))
        if key in month_keys:
            continue
        month_keys.add(key)
        if key in seen_before:
            repeat_month += 1
        else:
            new_month += 1

    return {
        "generated_at": compact_when_text(moment) or "",
        "repeats": repeats[:12],
        "hot_groups": hot[:12],
        "parent_child": parent_child[:12],
        "aging": aging[:12],
        "month": {
            "days": MONTH_DAYS,
            "incidents": len(month),
            "critical": int(severity.get("CRITICAL", 0)),
            "warning": int(severity.get("WARNING", 0)),
            "new_problems": new_month,
            "repeat_problems": repeat_month,
            "top_hosts": [{"hostname": name, "count": count} for name, count in hosts.most_common(10)],
        },
    }


def insight_sentences(facts: dict[str, Any]) -> list[str]:
    """Deterministic English conclusions. Shown when ForgeAI is off or the rewrite fails."""
    lines: list[str] = []
    repeats = facts.get("repeats") or []
    if repeats:
        top = repeats[0]
        lines.append(
            f"{top['hostname']} hit {top['alertname']} {top['count']} times in the last {top['days']} days "
            f"(latest {top['last']})."
        )
        if len(repeats) > 1:
            lines.append(f"{len(repeats)} assets repeated the same problem in the last {REPEAT_DAYS} days.")
    else:
        lines.append(f"No asset repeated the same problem {REPEAT_MIN} or more times in the last {REPEAT_DAYS} days.")

    hot = facts.get("hot_groups") or []
    if hot:
        item = hot[0]
        lines.append(
            f"{item['kind']} {item['name']} had {item['this_week']} incidents this week "
            f"(was {item['last_week']} last week)."
        )

    pile = facts.get("parent_child") or []
    if pile:
        item = pile[0]
        extra = f" plus {len(pile) - 1} more" if len(pile) > 1 else ""
        lines.append(f"Child {item['child']} is in problem while parent {item['parent']} is already open{extra}.")

    aging = facts.get("aging") or []
    if aging:
        item = aging[0]
        lines.append(
            f"{item['number']} on {item['hostname']} has been {item['status']} for {item['hours']}h without an ack."
        )

    month = facts.get("month") or {}
    if month.get("incidents"):
        lines.append(
            f"Last {month.get('days')} days: {month.get('incidents')} incidents "
            f"({month.get('critical')} critical, {month.get('warning')} warning); "
            f"{month.get('new_problems')} new problems, {month.get('repeat_problems')} seen before."
        )
    elif not lines:
        lines.append("No real incidents in the last 30 days. DEMO rows are excluded.")
    return lines[:5]


def _compact_facts(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "repeats": facts.get("repeats") or [],
        "hot_groups": facts.get("hot_groups") or [],
        "parent_child": facts.get("parent_child") or [],
        "aging": facts.get("aging") or [],
        "month": facts.get("month") or {},
    }


def last_insight_prose(db: Session) -> dict[str, Any] | None:
    row = (
        db.query(JournalEntry)
        .filter(JournalEntry.module == "insights", JournalEntry.action == "rewrite")
        .order_by(JournalEntry.id.desc())
        .first()
    )
    if row is None or not (row.detail or "").strip():
        return None
    sentences = [line.strip() for line in (row.detail or "").split("\n") if line.strip()]
    if not sentences:
        return None
    return {"sentences": sentences[:5], "summary": row.summary, "at": compact_when_text(row.at) or ""}


def rewrite_insights(db: Session, facts: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
    """Optional local LLM rewrite. Facts stay from SQL. Never invents hosts or counts."""
    from app.settings import settings
    from rca.llm import make_provider

    fallback = insight_sentences(facts)
    if not settings.ai_enabled or not settings.llm_url:
        report(
            db,
            "insights",
            "rewrite",
            "warn",
            summary="ForgeAI off — showing platform facts",
            detail="\n".join(fallback),
            object_type="insights",
            object_id="digest",
        )
        return {"sentences": fallback, "source": "facts", "hint": "ForgeAI is off. Showing platform facts."}

    timeout = min(float(settings.llm_timeout or 90), 20.0)
    llm = make_provider(settings.llm_url, settings.llm_model, timeout=timeout)
    blob = json.dumps(_compact_facts(facts), ensure_ascii=False)[:REWRITE_MAX]
    parsed = llm.complete_json(
        "You write 3 to 5 short NOC sentences from the JSON facts. "
        "Do not invent hosts, counts, or incidents. No commands. "
        'Return JSON {"sentences": ["...", "..."]}.',
        blob,
    )
    sentences: list[str] = []
    if isinstance(parsed, dict):
        raw = parsed.get("sentences") or parsed.get("lines") or []
        if isinstance(raw, list):
            sentences = [" ".join(str(item).split()) for item in raw if str(item).strip()]
        elif parsed.get("summary"):
            sentences = [" ".join(str(parsed.get("summary")).split())]
    if not sentences:
        why = getattr(llm, "last_error", "") or "no sentences"
        report(
            db,
            "insights",
            "rewrite",
            "error",
            summary="ForgeAI rewrite failed — showing platform facts",
            detail="\n".join(fallback),
            object_type="insights",
            object_id="digest",
        )
        return {"sentences": fallback, "source": "facts", "hint": f"ForgeAI rewrite failed ({why}). Showing platform facts."}

    text = "\n".join(sentences[:5])
    report(
        db,
        "insights",
        "rewrite",
        "ok",
        summary=f"ForgeAI digest ({len(sentences[:5])} sentences) by {actor}",
        detail=text,
        object_type="insights",
        object_id="digest",
    )
    return {"sentences": sentences[:5], "source": "forgeai", "hint": "ForgeAI rewrote these sentences from the facts below."}


def insight_panel(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    facts = insight_facts(db, now=now)
    prose = last_insight_prose(db)
    sentences = (prose or {}).get("sentences") or insight_sentences(facts)
    source = "forgeai" if prose else "facts"
    hint = (
        f"ForgeAI wrote this at {prose['at']}."
        if prose
        else "Platform facts. Ask ForgeAI to rewrite, or read the cards."
    )
    return {
        "facts": facts,
        "sentences": sentences,
        "source": source,
        "hint": hint,
        "ai_at": (prose or {}).get("at") or "",
    }


def insights_text(panel: dict[str, Any]) -> list[str]:
    from app.bulk import _block, _field, _join_docs

    facts = panel.get("facts") or {}
    month = facts.get("month") or {}
    body = [
        _field("generated", facts.get("generated_at")),
        _field("source", panel.get("source")),
        *_block("conclusions", "\n".join(panel.get("sentences") or [])),
        "",
        "Repeats",
    ]
    if not facts.get("repeats"):
        body.append(_field("repeats", "none"))
    for item in facts.get("repeats") or []:
        body.append(
            _field(
                "asset",
                f"{item.get('hostname')} — {item.get('alertname')} ×{item.get('count')} in {item.get('days')}d (last {item.get('last')})",
            )
        )
    body.extend(["", "Hot groups"])
    if not facts.get("hot_groups"):
        body.append(_field("groups", "none"))
    for item in facts.get("hot_groups") or []:
        body.append(
            _field(
                item.get("kind") or "group",
                f"{item.get('name')} this week {item.get('this_week')} / last week {item.get('last_week')}",
            )
        )
    body.extend(["", "Parent-child"])
    if not facts.get("parent_child"):
        body.append(_field("pairs", "none"))
    for item in facts.get("parent_child") or []:
        body.append(_field("pair", f"{item.get('child')} under {item.get('parent')} — {item.get('number')}"))
    body.extend(["", "Aging open"])
    if not facts.get("aging"):
        body.append(_field("aging", "none"))
    for item in facts.get("aging") or []:
        body.append(_field("open", f"{item.get('number')} {item.get('hostname')} {item.get('status')} {item.get('hours')}h"))
    body.extend(
        [
            "",
            "Month",
            _field("incidents", month.get("incidents")),
            _field("critical", month.get("critical")),
            _field("warning", month.get("warning")),
            _field("new", month.get("new_problems")),
            _field("repeat", month.get("repeat_problems")),
            _field("top hosts", ", ".join(f"{row['hostname']} ({row['count']})" for row in month.get("top_hosts") or []) or "—"),
        ]
    )
    return _join_docs("ForgeSRE ForgeAI digest (1)", [body], ["digest"])


def insights_csv(panel: dict[str, Any]) -> tuple[list[str], list[list[object]]]:
    facts = panel.get("facts") or {}
    rows: list[list[object]] = []
    for item in facts.get("repeats") or []:
        rows.append(["Repeat", item.get("hostname"), item.get("alertname"), item.get("count"), item.get("last")])
    for item in facts.get("hot_groups") or []:
        rows.append(["Hot group", f"{item.get('kind')} {item.get('name')}", "this vs last week", f"{item.get('this_week')}/{item.get('last_week')}", ""])
    for item in facts.get("parent_child") or []:
        rows.append(["Parent-child", item.get("child"), item.get("parent"), item.get("incident"), item.get("number")])
    for item in facts.get("aging") or []:
        rows.append(["Aging", item.get("hostname"), item.get("status"), f"{item.get('hours')}h", item.get("number")])
    month = facts.get("month") or {}
    rows.append(
        [
            "Month",
            f"{month.get('incidents')} incidents",
            f"{month.get('critical')} critical / {month.get('warning')} warning",
            f"{month.get('new_problems')} new / {month.get('repeat_problems')} repeat",
            ", ".join(f"{row['hostname']} ({row['count']})" for row in month.get("top_hosts") or []),
        ]
    )
    for line in panel.get("sentences") or []:
        rows.append(["Conclusion", line, "", "", ""])
    return (["Kind", "Where", "What", "Count", "Ref"], rows)
