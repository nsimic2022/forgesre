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
import time
import uuid
from typing import Any, Callable

from sqlalchemy.orm import Session, joinedload

from app.models import GoldenCase, GoldenRun, Incident, utcnow

JOB_KIND = "forgeai_golden"
COMPARE_KIND = "forgeai_compare"
LOAD_WAIT_S = 300
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
    pairs: dict[str, list[GoldenRun]] = {}
    for r in db.query(GoldenRun).filter(GoldenRun.compare != "").order_by(GoldenRun.id.desc()).limit(10).all():
        pairs.setdefault(r.compare, []).append(r)
    return {
        "comparisons": [{"id": cid, "runs": [run_dict(r) for r in sorted(rows, key=lambda r: r.id)]} for cid, rows in list(pairs.items())[:5]],
        "cases": [{"id": c.id, "incident": incidents[c.incident_id].number, "title": incidents[c.incident_id].title,
                   "cause": incidents[c.incident_id].root_cause or "", "expect": expected_words(c, incidents[c.incident_id])}
                  for c in cases if c.incident_id in incidents],
        "runs": [run_dict(r) for r in runs],
    }


def run_dict(row: GoldenRun) -> dict[str, Any]:
    return {"id": row.id, "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None, "actor": row.actor, "model": row.model,
            "skill": row.skill or "best match", "language": row.language, "passed": row.passed, "total": row.total,
            "results": row.results or [], "compare": row.compare or "", "model_file": row.model_file or "",
            "rejected": sum(1 for r in row.results or [] if r.get("status") not in ("trial", "used")),
            "avg_ms": int(sum(r.get("ms") or 0 for r in row.results or []) / max(1, len(row.results or [])))}


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
        results.append({"incident": incident.number.split("_")[0], "passed": ok, "status": call.status, "why": why if call.status in {"trial", "used"} else (call.reason or call.status),
                        "cause": str((call.answer or {}).get("cause") or "")[:300], "ms": call.duration_ms, "call_id": call.id,
                        "skill": call.skill_slug})
    row.results = results
    row.total = len(results)
    row.passed = sum(1 for r in results if r["passed"])
    row.finished_at = utcnow()
    db.commit()
    return row


def wait_for_model(name: str, timeout_s: int = LOAD_WAIT_S) -> bool:
    """Wait until llama.cpp serves the model file ``name`` (its /models lists it). Polls every 5 s."""
    import httpx

    from app.settings import settings

    base = (settings.llm_url or "").rstrip("/")
    stem = name.rsplit(".gguf", 1)[0].lower()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/models", timeout=5.0)
            ids = [str(m.get("id") or "") for m in (r.json().get("data") or [])] if r.status_code == 200 else []
            if any(stem in i.lower() or name.lower() in i.lower() for i in ids):
                return True
        except Exception:  # noqa: BLE001 - reloading: connection refused until the new model is up
            pass
        time.sleep(5)
    return False


def compare(db: Session, a: str, b: str, *, skill: str = "", actor: str = "system",
            wait: Callable[[str], bool] = wait_for_model) -> str:
    """Run the golden set on model file ``a``, then on ``b``, and put the first model back.

    Both runs share a compare id. The models are switched with llm_models.activate (llama.cpp
    reloads); thinking and the time limit as they were before are restored at the end. Runs in
    the LLM lane, so nothing else asks the model meanwhile. Returns the compare id.
    """
    from app.llm_models import activate, active_name, label, models_dir
    from app.settings import settings
    from app.system_settings import set_setting

    cid = uuid.uuid4().hex[:8]
    first = active_name(models_dir())
    thinking, timeout = settings.llm_thinking, int(settings.llm_timeout)
    try:
        for name in (a, b):
            activate(db, name, actor)
            if not wait(name):
                row = GoldenRun(actor=actor, skill=skill, compare=cid, model_file=name, model=label(name), finished_at=utcnow(),
                                results=[{"incident": "-", "passed": False, "status": "error", "why": "the model did not load in time", "ms": 0}])
                db.add(row)
                db.commit()
                continue
            row = run(db, skill=skill, actor=actor)
            row.compare, row.model_file, row.model = cid, name, label(name)
            db.commit()
    finally:
        if first:
            activate(db, first, actor)
            set_setting(db, "ai.llm.thinking", thinking, actor)
            set_setting(db, "ai.llm.timeout_seconds", timeout, actor)
            wait(first)
    return cid
