"""Noise filters: known noise is recorded, not paged (docs/alerting.md, rule 7).

A matching alarm still opens an incident, but in the quiet SUPPRESSED status with a
timeline note naming the filter, exactly like maintenance windows. Nobody is notified.

- action "quiet": quiet while the filter applies (its days and time window, below
  max_value, not expired, enabled).
- action "delay": quiet for the first ``delay_minutes`` of the incident, then it opens.
  This catches short spikes that clear on their own.

promote_filtered() runs on the escalation loop (every 30 s, leader only): an incident
whose filter no longer applies and that is still firing becomes OPEN and its mail ladder
starts, without waiting for Alertmanager's next repeat (up to 2 h).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Asset, Incident, NoiseFilter, utcnow

log = logging.getLogger("forgesre")

SCOPES = ("all", "asset", "type", "site")
ACTIONS = ("quiet", "delay")
NODE_PREFIX = "noise-"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _local(at: datetime) -> datetime:
    from app.dc_noc import _zone

    return _aware(at).astimezone(_zone())


def in_window(flt: NoiseFilter, at: datetime) -> bool:
    """Day and time window check in the appliance time zone. No window = always."""
    local = _local(at)
    if flt.days and local.weekday() not in {int(d) for d in flt.days}:
        return False
    if flt.start_minute is None or flt.end_minute is None:
        return True
    minute = local.hour * 60 + local.minute
    start, end = int(flt.start_minute), int(flt.end_minute)
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # crosses midnight


def in_scope(flt: NoiseFilter, asset: Asset | None) -> bool:
    if flt.scope == "all":
        return True
    if asset is None:
        return False
    want = (flt.scope_value or "").strip().lower()
    if flt.scope == "asset":
        return want in {(asset.hostname or "").lower(), (asset.asset_id or "").lower(), (asset.ip or "").lower()}
    if flt.scope == "type":
        return want == (asset.type or "").lower()
    if flt.scope == "site":
        from app.asset_extras import normalize_extras

        return want == normalize_extras(asset.extras).get("site", "").lower()
    return False


def filter_applies(
    flt: NoiseFilter, alertname: str, asset: Asset | None, sample: float | None, at: datetime, started_at: datetime | None = None
) -> bool:
    """True when this filter silences this alarm at ``at``. Pure: reads nothing else."""
    if not flt.enabled:
        return False
    if flt.expires_at is not None and _aware(flt.expires_at) <= _aware(at):
        return False
    names = {str(n).strip().lower() for n in flt.alertnames or [] if str(n).strip()}
    if names and (alertname or "").strip().lower() not in names:
        return False
    if not in_scope(flt, asset):
        return False
    if flt.max_value is not None and sample is not None and sample > float(flt.max_value):
        return False
    if flt.action == "delay":
        start = _aware(started_at or at)
        return _aware(at) < start + timedelta(minutes=max(1, int(flt.delay_minutes or 5)))
    return in_window(flt, at)


def matching_filter(
    db: Session, alertname: str, asset: Asset | None, sample: float | None, at: datetime, started_at: datetime | None = None
) -> NoiseFilter | None:
    """First enabled filter (by id) that silences this alarm, or None."""
    for flt in db.query(NoiseFilter).filter(NoiseFilter.enabled.is_(True)).order_by(NoiseFilter.id).all():
        if filter_applies(flt, alertname, asset, sample, at, started_at):
            return flt
    return None


def describe(flt: NoiseFilter) -> str:
    if flt.action == "delay":
        return f"Noise filter “{flt.name}”: waiting {flt.delay_minutes} min to see if it clears"
    return f"Noise filter “{flt.name}”: recorded, nobody notified" + (f" ({flt.note})" if flt.note else "")


def incident_alert(incident: Incident) -> tuple[str, float | None]:
    """Alert name and firing sample stored on the incident."""
    from app.services import alert_sample_value

    payload = incident.alert_payload if isinstance(incident.alert_payload, dict) else {}
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
    annotations = payload.get("annotations") if isinstance(payload.get("annotations"), dict) else {}
    return str(labels.get("alertname") or incident.title or ""), alert_sample_value(labels, annotations)


def filter_id_of(incident: Incident) -> int | None:
    """Id of the noise filter that last quieted this incident, if that was the last quiet reason."""
    last = None
    for item in incident.timeline or []:
        node = str(item.get("id") or "")
        if node.startswith(NODE_PREFIX) or node in {"maintenance", "parent", "quiet-clear"}:
            last = node
    if last and last.startswith(NODE_PREFIX):
        try:
            return int(last[len(NODE_PREFIX):])
        except ValueError:
            return None
    return None


def record_hit(flt: NoiseFilter) -> None:
    flt.hits = int(flt.hits or 0) + 1
    flt.last_hit_at = utcnow()


def promote_filtered(db: Session, now: datetime | None = None) -> int:
    """Open quiet incidents whose noise filter no longer applies; start their mail ladder.

    Returns how many were opened. One failing incident is logged and skipped.
    """
    from app.services import append_timeline, notify_first_step

    now = now or utcnow()
    opened = 0
    rows = db.query(Incident).options(joinedload(Incident.asset)).filter(Incident.status == "SUPPRESSED").all()
    for incident in rows:
        fid = filter_id_of(incident)
        if fid is None:
            continue
        try:
            flt = db.get(NoiseFilter, fid)
            alertname, sample = incident_alert(incident)
            if flt is not None and filter_applies(flt, alertname, incident.asset, sample, now, incident.started_at):
                continue
            incident.status = "OPEN"
            why = f"Noise filter “{flt.name}” no longer applies" if flt is not None else "Noise filter was removed"
            append_timeline(incident, "quiet-clear", "OPEN", f"{why}: still firing, mail ladder starts")
            db.flush()
            notify_first_step(db, incident)
            db.commit()
            opened += 1
        except Exception:
            db.rollback()
            log.exception("noise filter promotion failed for %s", incident.number)
    return opened


# --- CRUD used by the API ---------------------------------------------------------------


def as_dict(flt: NoiseFilter) -> dict[str, Any]:
    return {
        "id": flt.id,
        "name": flt.name,
        "enabled": flt.enabled,
        "alertnames": list(flt.alertnames or []),
        "scope": flt.scope,
        "scope_value": flt.scope_value,
        "days": list(flt.days or []),
        "start_minute": flt.start_minute,
        "end_minute": flt.end_minute,
        "max_value": flt.max_value,
        "action": flt.action,
        "delay_minutes": flt.delay_minutes,
        "expires_at": _aware(flt.expires_at).isoformat() if flt.expires_at else None,
        "owner": flt.owner,
        "note": flt.note,
        "hits": flt.hits,
        "last_hit_at": _aware(flt.last_hit_at).isoformat() if flt.last_hit_at else None,
        "created_by": flt.created_by,
    }


def apply_fields(flt: NoiseFilter, data: dict[str, Any]) -> NoiseFilter:
    """Validate and copy editable fields; ValueError with a short reason when invalid."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    scope = str(data.get("scope") or "all")
    if scope not in SCOPES:
        raise ValueError("scope must be all, asset, type or site")
    scope_value = str(data.get("scope_value") or "").strip()
    if scope != "all" and not scope_value:
        raise ValueError(f"say which {scope}")
    action = str(data.get("action") or "quiet")
    if action not in ACTIONS:
        raise ValueError("action must be quiet or delay")
    days = sorted({int(d) for d in data.get("days") or []})
    if any(d < 0 or d > 6 for d in days):
        raise ValueError("days are 0 (Monday) to 6 (Sunday)")
    start, end = data.get("start_minute"), data.get("end_minute")
    if (start is None) != (end is None):
        raise ValueError("give both start and end of the window, or neither")
    for value in (start, end):
        if value is not None and not 0 <= int(value) < 24 * 60:
            raise ValueError("window minutes are 0..1439")
    if action == "quiet" and start is None and not data.get("expires_at") and not days:
        # An always-on quiet filter with no end silences an alert for good: make it explicit.
        if not data.get("confirm_always"):
            raise ValueError("an always-on filter needs a time window, days or an end date")
    expires = data.get("expires_at")
    flt.name = name[:128]
    flt.enabled = bool(data.get("enabled", True))
    flt.alertnames = [str(n).strip() for n in data.get("alertnames") or [] if str(n).strip()]
    flt.scope = scope
    flt.scope_value = scope_value if scope != "all" else ""
    flt.days = days
    flt.start_minute = int(start) if start is not None else None
    flt.end_minute = int(end) if end is not None else None
    flt.max_value = float(data["max_value"]) if data.get("max_value") not in (None, "") else None
    flt.action = action
    flt.delay_minutes = max(1, min(int(data.get("delay_minutes") or 5), 240))
    flt.expires_at = datetime.fromisoformat(str(expires)) if expires else None
    if flt.expires_at is not None and flt.expires_at.tzinfo is None:
        from app.dc_noc import _zone

        flt.expires_at = flt.expires_at.replace(tzinfo=_zone())
    flt.owner = str(data.get("owner") or "").strip()[:255]
    flt.note = str(data.get("note") or "").strip()[:2000]
    return flt
