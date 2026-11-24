"""ForgeAI golden test set: resolved incidents with a known cause, replayed against the model.

An Engineer marks incidents whose confirmed cause is right (Settings › ForgeAI skills ›
Golden set, or ``POST /api/v1/forgeai/golden``). A run (job ``forgeai_golden``, LLM lane) asks
the "cause and first checks" task for each one as a trial, so nothing reaches the incident, and
scores a case as passed when the answer survives the safety checks and names one of the expected
words (by default the longer words of the confirmed cause). Runs are kept to compare models,
skills and the answer language over time.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import GoldenCase, GoldenRun, Incident, utcnow

JOB_KIND = "forgeai_golden"
MAX_CASES = 30
_STOP = {"the", "and", "was", "were", "with", "from", "that", "this", "after", "before", "because", "into", "when",
         "which", "then", "than", "have", "had", "has", "not", "for", "on", "of", "to", "in", "a", "an", "is"}


def expected_words(case: GoldenCase, incident: Incident) -> list[str]:
    """The words a good answer should contain: ``expect`` (comma separated) or the cause's longer words."""
    if case.expect.strip():
        return [w.strip().lower() for w in case.expect.split(",") if w.strip()]
    words = re.findall(r"[a-z0-9][a-z0-9_./-]{3,}", (incident.root_cause or "").lower())
    return [w for w in dict.fromkeys(words) if w not in _STOP][:8]


def add_case(db: Session, number: str, expect: str, actor: str) -> GoldenCase:
    """Add an incident; it needs a confirmed cause or expected words."""
    incident = db.query(Incident).filter(Incident.number.like(f"{number.strip()}%")).order_by(Incident.id.desc()).first()
    if incident is None:
        raise LookupError("incident not found")
    if not (incident.root_cause or "").strip() and not expect.strip():
        raise ValueError("the incident has no confirmed cause; give the expected words")
    if db.query(GoldenCase).filter_by(incident_id=incident.id).first():
        raise ValueError("already in the golden set")
    if db.query(GoldenCase).count() >= MAX_CASES:
        raise ValueError(f"the golden set holds at most {MAX_CASES} incidents")
    row = GoldenCase(incident_id=incident.id, expect=expect.strip()[:300], added_by=actor)
    db.add(row)
    db.commit()
    return row


def overview(db: Session) -> dict[str, Any]:
    cases = db.query(GoldenCase).order_by(GoldenCase.id).all()
    incidents = {i.id: i for i in db.query(Incident).filter(Incident.id.in_([c.incident_id for c in cases] or [-1])).all()}
    runs = db.query(GoldenRun).order_by(GoldenRun.id.desc()).limit(10).all()
    return {
        "cases": [{"id": c.id, "incident": incidents[c.incident_id].number, "title": incidents[c.incident_id].title,
                   "cause": incidents[c.incident_id].root_cause or "", "expect": expected_words(c, incidents[c.incident_id])}
                  for c in cases if c.incident_id in incidents],
        "runs": [run_dict(r) for r in runs],
    }


def run_dict(row: GoldenRun) -> dict[str, Any]:
    return {"id": row.id, "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None, "actor": row.actor, "model": row.model,
            "skill": row.skill or "best match", "language": row.language, "passed": row.passed, "total": row.total,
            "results": row.results or []}


def score(call_status: str, answer: dict[str, Any], words: list[str]) -> tuple[bool, str]:
    """Passed when the answer passed the checks and names an expected word."""
    if call_status not in {"trial", "used"}:
        return False, "rejected or failed"
    text = " ".join([str(answer.get("cause") or ""), *map(str, answer.get("first_checks") or [])]).lower()
    hit = next((w for w in words if w in text), "")
    return (bool(hit), f"names '{hit}'" if hit else "no expected word in the answer")


def run(db: Session, *, skill: str = "", actor: str = "system") -> GoldenRun:
    """Replay every case once (trial calls) and store the run. Each call has the model's own timeout."""
    from app import forgeai

    row = GoldenRun(actor=actor, skill=skill, language=forgeai.language())
    db.add(row)
    db.commit()
    results = []
    for case in db.query(GoldenCase).order_by(GoldenCase.id).all():
        incident = db.query(Incident).options(joinedload(Incident.asset)).filter_by(id=case.incident_id).first()
        if incident is None:
            continue
        call = forgeai.run(db, "incident", incident, skill_slug=skill, actor=f"golden:{actor}", trial=True)
        ok, why = score(call.status, call.answer or {}, expected_words(case, incident))
        row.model = row.model or call.model
        results.append({"incident": incident.number.split("_")[0], "passed": ok, "why": why if call.status in {"trial", "used"} else (call.reason or call.status),
                        "cause": str((call.answer or {}).get("cause") or "")[:300], "ms": call.duration_ms, "call_id": call.id,
                        "skill": call.skill_slug})
    row.results = results
    row.total = len(results)
    row.passed = sum(1 for r in results if r["passed"])
    row.finished_at = utcnow()
    db.commit()
    return row
