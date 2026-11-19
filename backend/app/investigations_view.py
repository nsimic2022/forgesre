"""Investigations page of the new UI and the analyst review that feeds it.

An analyst confirms the real cause of an incident and rates ForgeRCA and ForgeAI
(correct / partly / wrong). Those reviews give the accuracy figures, the recurring-cause
list and, later, the knowledge base ForgeAI 2 searches (docs/roadmap.md).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.audit import audit
from app.models import Asset, Incident, utcnow
from app.noc import RESOLVED

RATINGS = ("correct", "partly", "wrong")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _minutes(incident: Incident) -> int | None:
    start, end = incident.started_at, incident.resolved_at or incident.ended_at
    if not start or not end:
        return None
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() // 60))


def _split(incident: Incident) -> tuple[Any, Any]:
    rca = ai = None
    for inv in incident.investigations or []:
        if (inv.provider or "").endswith("-llm"):
            ai = inv
        else:
            rca = inv
    return rca, ai


def review_incident(db: Session, incident: Incident, actor: str, root_cause: str, rca_rating: str, ai_rating: str) -> dict[str, Any]:
    """Store the analyst's confirmed cause and ratings on the incident; audited.

    Ratings must be correct, partly, wrong or empty (not rated). The cause is required.
    """
    cause = (root_cause or "").strip()
    if not cause:
        raise ValueError("write the confirmed cause")
    for value in (rca_rating, ai_rating):
        if value and value not in RATINGS:
            raise ValueError("rating must be correct, partly or wrong")
    incident.root_cause = cause[:4000]
    incident.rca_rating = rca_rating or ""
    incident.ai_rating = ai_rating or ""
    incident.reviewed_by = actor
    incident.reviewed_at = utcnow()
    audit(
        db,
        "incident.review",
        actor=actor,
        object_type="incident",
        object_id=incident.number,
        data={"root_cause": incident.root_cause, "rca": incident.rca_rating, "ai": incident.ai_rating},
        commit=True,
    )
    return review_of(incident)


def review_of(incident: Incident) -> dict[str, Any]:
    return {
        "root_cause": incident.root_cause or "",
        "rca_rating": incident.rca_rating or "",
        "ai_rating": incident.ai_rating or "",
        "reviewed_by": incident.reviewed_by or "",
        "reviewed_at": _iso(incident.reviewed_at),
    }


def _accuracy(rows: list[Incident], field: str) -> dict[str, Any]:
    rated = [getattr(r, field) for r in rows if getattr(r, field)]
    if not rated:
        return {"rated": 0, "correct": 0, "partly": 0, "wrong": 0, "score": None}
    counts = {k: rated.count(k) for k in RATINGS}
    # "partly" counts half: it found something useful but not the whole cause.
    score = round(100 * (counts["correct"] + 0.5 * counts["partly"]) / len(rated))
    return {"rated": len(rated), **counts, "score": score}


def investigations_page(
    db: Session, *, days: int = 90, q: str = "", only_ai: bool = False, only_reviewed: bool = False, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Resolved incidents of the last ``days`` with their analyses and reviews, plus accuracy
    and recurring confirmed causes over the same period."""
    since = utcnow() - timedelta(days=max(1, min(int(days), 730)))
    query = (
        db.query(Incident)
        .outerjoin(Asset, Incident.asset_id == Asset.id)
        .options(joinedload(Incident.asset), selectinload(Incident.investigations))
        .filter(Incident.status.in_(RESOLVED), Incident.started_at >= since)
    )
    period = query.all()
    text = q.strip()
    if text:
        like = f"%{text}%"
        query = query.filter(
            or_(Incident.number.ilike(like), Incident.title.ilike(like), Asset.hostname.ilike(like), Incident.root_cause.ilike(like))
        )
    rows = query.order_by(Incident.started_at.desc()).all()
    out = []
    for incident in rows:
        rca, ai = _split(incident)
        if only_ai and ai is None:
            continue
        if only_reviewed and not incident.root_cause:
            continue
        out.append(
            {
                "number": incident.number,
                "title": incident.title,
                "severity": (incident.severity or "").upper(),
                "started_at": _iso(incident.started_at),
                "minutes": _minutes(incident),
                "host": incident.asset.hostname if incident.asset else "",
                "forgerca": (rca.likely_cause if rca else "") or "",
                "forgeai": bool(ai),
                **review_of(incident),
            }
        )
    recurring: dict[str, list[str]] = defaultdict(list)
    for incident in period:
        if incident.root_cause:
            recurring[incident.root_cause.strip().lower()].append(incident.asset.hostname if incident.asset else "")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return {
        "days": days,
        "total": len(out),
        "offset": offset,
        "limit": limit,
        "rows": out[offset : offset + limit],
        "accuracy": {"forgerca": _accuracy(period, "rca_rating"), "forgeai": _accuracy(period, "ai_rating")},
        "reviewed": sum(1 for r in period if r.root_cause),
        "resolved": len(period),
        "recurring": sorted(
            (
                {"cause": next(i.root_cause for i in period if i.root_cause.strip().lower() == key), "count": len(hosts), "hosts": sorted({h for h in hosts if h})[:5]}
                for key, hosts in recurring.items()
                if len(hosts) >= 2
            ),
            key=lambda item: -item["count"],
        )[:10],
    }
