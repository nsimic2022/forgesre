"""Heartbeats of the background loops, in the database so any process can report on them.

Core may run several web workers and an optional separate worker process (``app.worker``).
Only the leader runs the loops (``app.leader``); each loop writes its beat here, so System
Health, ``./forgesre doctor`` and ``./forgesre status`` see the same answer whichever process
serves the request. A beat is written at most every ``MIN_GAP`` seconds per loop.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any

from app.models import LoopBeat, utcnow

MIN_GAP = 20.0
# Longest normal gap between two beats of each loop, with room for one slow round.
EXPECTED = {"jobs": 120, "llm-jobs": 120, "escalation": 180, "discovery": 6 * 3600 + 300}
_last: dict[str, float] = {}


def process_name() -> str:
    role = os.environ.get("FORGESRE_ROLE", "all").strip() or "all"
    return f"{role}@{socket.gethostname()}:{os.getpid()}"[:128]


def beat(name: str, *, leader: bool = True) -> None:
    """Record that loop ``name`` ran now in this process. Never raises."""
    now = time.monotonic()
    if now - _last.get(name, 0.0) < MIN_GAP:
        return
    _last[name] = now
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            row = db.get(LoopBeat, name)
            if row is None:
                row = LoopBeat(name=name)
                db.add(row)
            row.process, row.leader, row.at = process_name(), leader, utcnow()
            db.commit()
        finally:
            db.close()
    except Exception:
        _last.pop(name, None)


def age_seconds(name: str) -> float | None:
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            row = db.get(LoopBeat, name)
            if row is None or row.at is None:
                return None
            at = row.at if row.at.tzinfo else row.at.replace(tzinfo=utcnow().tzinfo)
            return max(0.0, (utcnow() - at).total_seconds())
        finally:
            db.close()
    except Exception:
        return None


def status() -> dict[str, Any]:
    """Doctor row: every loop's age and process. warn when one is late, or nothing beat at all."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        rows = {r.name: r for r in db.query(LoopBeat).all()}
    finally:
        db.close()
    now = utcnow()
    loops, late = [], []
    for name, limit in EXPECTED.items():
        row = rows.get(name)
        if row is None:
            loops.append({"name": name, "age_s": None, "process": ""})
            continue
        at = row.at if row.at.tzinfo else row.at.replace(tzinfo=now.tzinfo)
        age = int((now - at).total_seconds())
        loops.append({"name": name, "age_s": age, "process": row.process})
        if age > limit:
            late.append(f"{name} {age}s ago")
    seen = [loop for loop in loops if loop["age_s"] is not None]
    if not seen:
        return {"status": "warn", "why": "No background loop has reported yet (Core just started, or no worker is running).",
                "fix": "docker compose logs core worker", "loops": loops}
    leaders = sorted({loop["process"] for loop in seen})
    if late:
        return {"status": "warn", "why": f"Late: {', '.join(late)}. Running in {', '.join(leaders)}.",
                "fix": "docker compose logs core worker; ./forgesre jobs", "loops": loops}
    return {"status": "ok", "why": f"Loops on time in {', '.join(leaders)}.", "loops": loops}
