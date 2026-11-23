"""Warranty / support, vendor end of life and licence expiry per asset, and the daily-style check.

Dates live in asset extras: ``support_to`` (with ``support_lead_days``), ``eol_date`` and
``license_to``. ``check_lifecycle`` runs inside the 15-minute ``health_checks`` job and fires
``AssetLifecycleExpiring`` per asset and date (warning while expiring; an expired licence is
high because features stop working); a date moved forward resolves the incident.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.asset_extras import SUPPORT_LEAD_DEFAULT, _iso_date, normalize_extras
from app.models import Asset
from app.settings import settings

ALERTNAME = "AssetLifecycleExpiring"
KINDS = {"support": "Support / warranty", "eol": "End of life", "license": "Licence"}
_FIELDS = {"support": "support_to", "eol": "eol_date", "license": "license_to"}


def warn_days() -> int:
    """Days ahead to warn for EoL and licences (support uses the asset's own lead days)."""
    raw = ((settings.yaml or {}).get("checks") or {}).get("lifecycle_warn_days")
    try:
        return max(1, min(365, int(raw)))
    except (TypeError, ValueError):
        return 30


def items_for(asset: Any, today: date | None = None, ahead: int | None = None) -> list[dict[str, Any]]:
    """One row per filled date: kind, date, days_left, status ok | expiring | expired."""
    extras = normalize_extras(getattr(asset, "extras", None))
    today = today or date.today()
    ahead = warn_days() if ahead is None else ahead
    out = []
    for kind, key in _FIELDS.items():
        when = _iso_date(extras.get(key))
        if when is None:
            continue
        lead = int(extras.get("support_lead_days") or SUPPORT_LEAD_DEFAULT) if kind == "support" else ahead
        left = (when - today).days
        status = "expired" if left < 0 else "expiring" if left <= lead else "ok"
        out.append({"asset": asset.asset_id, "hostname": asset.hostname, "kind": kind, "label": KINDS[kind],
                    "date": when.isoformat(), "days_left": left, "status": status})
    return out


def overview(db: Session, today: date | None = None) -> dict[str, Any]:
    """Every expiring or expired date in inventory, soonest first, plus how many dates are tracked."""
    rows: list[dict[str, Any]] = []
    tracked = 0
    for asset in db.query(Asset).order_by(Asset.asset_id).all():
        items = items_for(asset, today)
        tracked += len(items)
        rows.extend(i for i in items if i["status"] != "ok")
    rows.sort(key=lambda r: r["days_left"])
    return {"rows": rows, "tracked": tracked, "warn_days": warn_days()}


def _detail(item: dict[str, Any]) -> str:
    left = item["days_left"]
    when = f"expired {-left} days ago" if left < 0 else "expires today" if left == 0 else f"expires in {left} days"
    return f"{item['label']} of {item['hostname']} {when} ({item['date']})"


def check_lifecycle(db: Session, today: date | None = None) -> list[dict[str, Any]]:
    """Alertmanager-shaped alerts for every tracked date; ok dates resolve earlier incidents."""
    from app.health_checks import _save
    from app.models import utcnow

    alerts = []
    for asset in db.query(Asset).order_by(Asset.asset_id).all():
        for item in items_for(asset, today):
            target = f"{item['asset']}:{item['kind']}"
            status = {"ok": "ok", "expiring": "warning"}.get(item["status"], "error" if item["kind"] == "license" else "warning")
            detail = _detail(item)
            _save(db, "lifecycle", target, status, float(item["days_left"]), detail)
            severity = {"warning": "warning", "error": "high"}.get(status, "warning")
            alerts.append({
                "status": "resolved" if status == "ok" else "firing",
                "labels": {"alertname": ALERTNAME, "asset": item["asset"], "severity": severity, "job": "forgesre-checks",
                           "check": "lifecycle", "target": target, "lifecycle": item["kind"]},
                "annotations": {"summary": f"{item['label']} {item['status']} on {item['asset']}", "description": detail},
                "startsAt": utcnow().isoformat(),
            })
    db.flush()  # the session does not autoflush; callers read check_results right after
    return alerts
