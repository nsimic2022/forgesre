"""Local knowledge for ForgeAI: our own incidents with an analyst-confirmed cause, and playbooks.

Search is exact first, then by words: the same alert name scores highest, then the same asset,
the same asset type and the overlap of title and cause words. Everything stays in the
ForgeSRE database; nothing is sent anywhere. (Semantic search with pgvector and vendor
documents are on the roadmap; this keeps the same interface so they can be added behind it.)
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Incident, Playbook

STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "was", "for", "with", "by", "at", "it",
        "high", "low", "alert", "alarm", "down", "up", "from", "this", "that", "not"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_/.-]{2,}", (text or "").lower()) if w not in STOP}


def alert_name(incident: Incident) -> str:
    return (incident.fingerprint or "").split(":", 1)[0]


def similar_confirmed(db: Session, incident: Incident, *, limit: int = 5, pool: int = 400) -> list[dict[str, Any]]:
    """Up to ``limit`` resolved incidents with a confirmed cause, most alike first, with why they match."""
    rows = (db.query(Incident).options(joinedload(Incident.asset))
            .filter(Incident.root_cause != "", Incident.id != incident.id)
            .order_by(Incident.id.desc()).limit(pool).all())
    alert = alert_name(incident).lower()
    words = _words(f"{incident.title} {alert}")
    asset = incident.asset
    scored: list[tuple[float, Incident, list[str]]] = []
    for row in rows:
        score, why = 0.0, []
        if alert and alert_name(row).lower() == alert:
            score, why = score + 3, why + ["same alert"]
        if asset is not None and row.asset_id == asset.id:
            score, why = score + 2, why + ["same asset"]
        elif asset is not None and row.asset is not None and row.asset.type == asset.type:
            score, why = score + 1, why + [f"same type ({asset.type})"]
        theirs = _words(f"{row.title} {alert_name(row)} {row.root_cause}")
        shared = words & theirs
        if shared:
            score += 2 * len(shared) / max(1, len(words))
            why.append("similar words")
        if row.ai_rating == "wrong":
            score -= 0.1  # keep, but a confirmed cause behind a wrong ForgeAI answer is not a better example
        if score >= 1:
            scored.append((score, row, why))
    scored.sort(key=lambda t: (-t[0], -t[1].id))
    return [{
        "incident": r.number.split("_")[0], "title": r.title, "host": r.asset.hostname if r.asset else "",
        "confirmed_cause": r.root_cause, "why": ", ".join(why), "score": round(s, 2),
    } for s, r, why in scored[:limit]]


def playbooks_for(db: Session, incident: Incident, *, limit: int = 2) -> list[dict[str, Any]]:
    """The incident's own playbook first, then others whose name shares words with the alert."""
    out: list[dict[str, Any]] = []
    if incident.playbook is not None:
        out.append({"name": incident.playbook.name, "steps": [str(s.get("title") if isinstance(s, dict) else s) for s in incident.playbook.steps or []][:8]})
    words = _words(f"{incident.title} {alert_name(incident)}")
    for pb in db.query(Playbook).all():
        if len(out) >= limit:
            break
        if incident.playbook is not None and pb.id == incident.playbook.id:
            continue
        if words & _words(f"{pb.name} {pb.description or ''}"):
            out.append({"name": pb.name, "steps": [str(s.get("title") if isinstance(s, dict) else s) for s in pb.steps or []][:8]})
    return out
