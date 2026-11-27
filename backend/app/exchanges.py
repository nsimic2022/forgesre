"""Record and list calls between ForgeSRE and outside systems (IntegrationExchange).

``record`` is safe to call from anywhere, also from a thread or a client library: it opens its
own session, never raises, and stores no token or full body. The newest 1000 rows per
integration are kept.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger("forgesre")

KEEP = 1000
_count = 0
_lock = threading.Lock()


def record(integration: str, direction: str, kind: str, *, endpoint: str = "", peer: str = "", status: str = "ok",
           http_status: int = 0, ms: int = 0, summary: str = "", detail: dict[str, Any] | None = None) -> None:
    """Store one exchange in its own session. Failing to store it must never break the call itself."""
    global _count
    from app.db import SessionLocal
    from app.models import IntegrationExchange

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001 — logging a call must never break the call itself
        return
    try:
        db.add(IntegrationExchange(
            integration=integration[:32], direction=direction[:8], kind=kind[:64], endpoint=endpoint[:255], peer=peer[:128],
            status=status[:16], http_status=int(http_status or 0), ms=int(ms or 0), summary=summary[:500], detail=detail or {},
        ))
        db.commit()
        with _lock:
            _count += 1
            prune = _count % 50 == 0
        if prune:
            _prune(db, integration)
    except Exception:
        db.rollback()
        log.debug("could not record an integration exchange", exc_info=True)
    finally:
        db.close()


def _prune(db: Session, integration: str) -> None:
    from app.models import IntegrationExchange

    cutoff = (db.query(IntegrationExchange.id).filter(IntegrationExchange.integration == integration)
              .order_by(IntegrationExchange.id.desc()).offset(KEEP).limit(1).scalar())
    if cutoff:
        db.query(IntegrationExchange).filter(IntegrationExchange.integration == integration, IntegrationExchange.id <= cutoff).delete(synchronize_session=False)
        db.commit()


def as_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "at": row.at.isoformat() if row.at else None, "direction": row.direction, "kind": row.kind,
        "endpoint": row.endpoint, "peer": row.peer, "status": row.status, "http_status": row.http_status,
        "ms": row.ms, "summary": row.summary, "detail": row.detail or {},
    }


def list_exchanges(db: Session, integration: str, *, direction: str = "", kinds: list[str] | None = None, status: str = "",
                   since: datetime | None = None, after_id: int = 0, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Newest first, filtered; ``total`` for the pager."""
    from app.models import IntegrationExchange

    q = db.query(IntegrationExchange).filter(IntegrationExchange.integration == integration)
    if direction:
        q = q.filter(IntegrationExchange.direction == direction)
    if kinds:
        q = q.filter(IntegrationExchange.kind.in_(kinds))
    if status:
        q = q.filter(IntegrationExchange.status.in_(status.split(",")))
    if since is not None:
        q = q.filter(IntegrationExchange.at >= since)
    if after_id:
        q = q.filter(IntegrationExchange.id > after_id)
    total = q.count()
    rows = q.order_by(IntegrationExchange.id.desc()).offset(max(0, offset)).limit(max(1, min(limit, 200))).all()
    return {"total": total, "rows": [as_dict(r) for r in rows]}


def last_id(db: Session, integration: str) -> int:
    from app.models import IntegrationExchange

    return int(db.query(IntegrationExchange.id).filter(IntegrationExchange.integration == integration)
               .order_by(IntegrationExchange.id.desc()).limit(1).scalar() or 0)
