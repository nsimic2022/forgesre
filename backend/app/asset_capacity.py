"""30-day capacity for one asset: disk use and interface receive, with a "days to full" forecast.

The numbers come from ``dc_noc.capacity_panel`` (Prometheus range queries, Zabbix trends when
Prometheus has no disk series). Those reads can take seconds, so they run as the ``asset_capacity``
job; the finished job is the cache. A result younger than ``FRESH`` is served as it is.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import Asset, Job, utcnow

CAPACITY_KIND = "asset_capacity"
FRESH = timedelta(minutes=15)
WARN_DAYS = 30


def _aware(value):
    if value is not None and value.tzinfo is None:
        from datetime import timezone

        return value.replace(tzinfo=timezone.utc)
    return value


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    days = [int(r["forecast_days"]) for r in rows if r.get("forecast_days") is not None]
    soonest = min(days) if days else None
    return {"soonest_days": soonest, "warn": soonest is not None and soonest <= WARN_DAYS}


def capacity_view(db: Session, asset: Asset, *, refresh: bool = False) -> dict[str, Any]:
    """Latest capacity result for the asset, queueing a new job when it is missing or stale.

    ``state`` is ``ready`` (rows are here), ``pending`` (a job is queued or running; poll again)
    or ``error`` (the last job failed; a new one is queued on the next call or with refresh).
    """
    from app.jobs import enqueue

    last = (
        db.query(Job)
        .filter(Job.kind == CAPACITY_KIND, Job.object_id == asset.asset_id)
        .order_by(Job.id.desc())
        .first()
    )
    running = last is not None and last.status in ("pending", "running")
    fresh = (
        last is not None
        and last.status == "done"
        and last.finished_at is not None
        and utcnow() - _aware(last.finished_at) < FRESH
    )
    if running:
        return {"state": "pending", "job": last.id}
    if fresh and not refresh:
        result = dict((last.payload or {}).get("result") or {})
        rows = list(result.get("rows") or [])
        return {"state": "ready", "rows": rows, "measured_at": last.finished_at.isoformat(), **_summary(rows)}
    failed = last is not None and last.status == "error" and not refresh
    job = enqueue(db, CAPACITY_KIND, asset.asset_id, object_type="asset", payload={})
    if failed:
        return {"state": "error", "error": last.error or "The capacity read failed.", "job": job.id if job else None}
    return {"state": "pending", "job": job.id if job else None}


def run_capacity(db: Session, asset_id: str) -> dict[str, Any]:
    """Job body: read ~30 days for the asset. Each Prometheus query has a 3 s timeout, Zabbix its own."""
    from app.dc_noc import capacity_panel

    asset = db.query(Asset).filter_by(asset_id=asset_id).first()
    if asset is None:
        raise RuntimeError(f"asset {asset_id} not found")
    panel = capacity_panel(asset)
    return {"rows": list(panel.get("rows") or []), "start": panel.get("start"), "end": panel.get("end")}
