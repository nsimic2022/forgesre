"""ForgeAI tuning: how a skill does on real incidents, and a replay to try new instructions.

``tuning_report`` puts each incident answer of a skill next to what the analyst confirmed
(root cause and the correct / partly / wrong rating on the incident), with the thumbs and the
reasons answers were rejected. That is what an Engineer reads before changing the instructions.

``replay`` (job ``forgeai_replay``, LLM lane) asks the skill again, saved or unsaved text, on the
last resolved incidents that have a confirmed cause. Every answer is a trial: it is kept in the
ForgeAI log but never becomes an incident's answer. The word overlap with the confirmed cause is
only a hint for sorting; the analyst decides whether the new text is better.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import ForgeAiCall, ForgeAiSkill, Incident, utcnow

REPLAY_KIND = "forgeai_replay"
REPLAY_MAX = 20
REPLAY_BUDGET_S = 900
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "was", "for", "with", "by", "at", "it", "its",
        "be", "has", "had", "not", "no", "from", "this", "that", "as", "are", "were", "after", "before", "because"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9._/-]{2,}", (text or "").lower()) if w not in STOP}


def overlap(answer: str, confirmed: str) -> float:
    """Share of the confirmed cause's words that the answer also uses (0..1). A sorting hint, not a verdict."""
    want = _words(confirmed)
    return round(len(want & _words(answer)) / len(want), 2) if want else 0.0


def _confirmed(db: Session, limit: int, skill: ForgeAiSkill | None = None) -> list[Incident]:
    from app.forgeai import pick_skill

    rows = (db.query(Incident).filter(Incident.root_cause != "", Incident.status.in_(("RESOLVED", "CLOSED")))
            .order_by(Incident.id.desc()).limit(limit * 5).all())
    if skill is not None and (skill.match_alerts or skill.match_types):
        rows = [r for r in rows if (pick := pick_skill(db, skill.task, r)) is not None and pick.slug == skill.slug]
    return rows[:limit]


def tuning_report(db: Session, slug: str, days: int = 60) -> dict[str, Any]:
    """Answers of one skill against the analysts' confirmed causes, rejection reasons and thumbs."""
    skill = db.query(ForgeAiSkill).filter_by(slug=slug).first()
    if skill is None:
        raise LookupError("skill not found")
    since = utcnow() - timedelta(days=days)
    calls = (db.query(ForgeAiCall).filter(ForgeAiCall.skill_slug == slug, ForgeAiCall.created_at >= since,
                                          ForgeAiCall.status != "trial").order_by(ForgeAiCall.id.desc()).all())
    reasons = Counter(re.sub(r"\d+", "N", c.reason or "no reason")[:120] for c in calls if c.status == "rejected")
    errors = Counter((c.reason or "no reason")[:120] for c in calls if c.status == "error")
    ids = {c.incident_id for c in calls if c.incident_id}
    incidents = {i.id: i for i in db.query(Incident).filter(Incident.id.in_(ids)).all()} if ids else {}
    seen: set[int] = set()
    examples: list[dict[str, Any]] = []
    ratings = Counter()
    for c in calls:
        inc = incidents.get(c.incident_id or 0)
        if c.status != "used" or inc is None or inc.id in seen:
            continue
        seen.add(inc.id)
        if inc.ai_rating:
            ratings[inc.ai_rating] += 1
        said = str((c.answer or {}).get("cause") or (c.answer or {}).get("paragraph") or (c.answer or {}).get("summary") or "")
        examples.append({
            "incident": inc.number.split("_")[0], "title": inc.title, "said": said, "confirmed": inc.root_cause or "",
            "rating": inc.ai_rating or "", "thumbs": c.rating, "overlap": overlap(said, inc.root_cause) if inc.root_cause else None,
            "call_id": c.id, "skill_version": c.skill_version,
        })
    order = {"wrong": 0, "partly": 1, "": 2, "correct": 3}
    examples.sort(key=lambda e: (order.get(e["rating"], 2), e["thumbs"]))
    rated = sum(ratings.values())
    return {
        "skill": slug, "name": skill.name, "task": skill.task, "version": skill.version, "days": days,
        "calls": len(calls), "used": sum(c.status == "used" for c in calls),
        "rejected": sum(c.status == "rejected" for c in calls), "errors": sum(c.status == "error" for c in calls),
        "thumbs_up": sum(c.rating > 0 for c in calls), "thumbs_down": sum(c.rating < 0 for c in calls),
        "ratings": dict(ratings), "correct_share": round(ratings.get("correct", 0) / rated, 2) if rated else None,
        "reject_reasons": [{"reason": r, "count": n} for r, n in reasons.most_common(6)],
        "error_reasons": [{"reason": r, "count": n} for r, n in errors.most_common(4)],
        "examples": examples[:25],
        "replay_candidates": len(_confirmed(db, REPLAY_MAX, skill)) if skill.task == "incident" else 0,
    }


def replay(db: Session, slug: str, *, limit: int = 10, instructions: str | None = None, actor: str = "system") -> dict[str, Any]:
    """Job body: ask the skill again on up to ``limit`` confirmed incidents, one at a time, within a time budget."""
    from app import forgeai

    skill = db.query(ForgeAiSkill).filter_by(slug=slug).first()
    if skill is None:
        raise LookupError("skill not found")
    if skill.task != "incident":
        raise ValueError("replay needs a skill for incident answers (cause and first checks)")
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    for inc in _confirmed(db, max(1, min(int(limit or 10), REPLAY_MAX)), skill):
        if time.monotonic() - started > REPLAY_BUDGET_S:
            rows.append({"incident": inc.number.split("_")[0], "status": "skipped", "reason": "time budget used up"})
            continue
        call = forgeai.run(db, "incident", inc, skill_slug=slug, actor=actor, trial=True, instructions=instructions)
        said = str((call.answer or {}).get("cause") or "")
        before = (db.query(ForgeAiCall).filter(ForgeAiCall.incident_id == inc.id, ForgeAiCall.status == "used")
                  .order_by(ForgeAiCall.id.desc()).first())
        rows.append({
            "incident": inc.number.split("_")[0], "title": inc.title, "confirmed": inc.root_cause, "rating": inc.ai_rating or "",
            "status": call.status, "reason": call.reason, "said": said, "overlap": overlap(said, inc.root_cause) if call.status == "trial" else None,
            "before": str((before.answer or {}).get("cause") or "") if before else "",
            "before_overlap": overlap(str((before.answer or {}).get("cause") or ""), inc.root_cause) if before else None,
            "call_id": call.id,
        })
    answered = [r for r in rows if r.get("overlap") is not None]
    return {
        "skill": slug, "unsaved_text": instructions is not None, "asked": len(rows), "answered": len(answered),
        "rejected": sum(r["status"] == "rejected" for r in rows), "errors": sum(r["status"] == "error" for r in rows),
        "mean_overlap": round(sum(r["overlap"] for r in answered) / len(answered), 2) if answered else None,
        "rows": rows, "seconds": int(time.monotonic() - started),
    }
