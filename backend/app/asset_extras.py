"""V0.8 NOC fields on an asset (assets.extras JSON) and the per-asset playrule preference (assets.playrule_ids).

Local Core data only — never written to NetBox. Playrule ids are a preference for which enabled
playrule handles an alertname on this host; they never create or change Prometheus rules.

Support coverage (support / support_custom / support_from / support_to / support_lead_days) lives in the same JSON.
Its status is derived on read for the UI and mail bodies only — no alert, incident, or Playrule.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

# key, English label, max length
EXTRA_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("customer", "Customer / domain", 255),
    ("site", "Site / DC / room", 255),
    ("backup_name", "Backup on-call name", 255),
    ("backup_phone", "Backup on-call phone", 64),
    ("backup_email", "Backup on-call email", 255),
    ("support_hours", "Support hours", 128),
    ("timezone", "Timezone", 64),
    ("contract", "License / contract / SLA", 500),
    ("runbook_note", "Runbook note", 4000),
)
DISPLAY_KEYS = tuple(key for key, _label, _max in EXTRA_FIELDS)
SUPPORT_KEYS = ("support", "support_custom", "support_from", "support_to", "support_lead_days")
EXTRA_KEYS = DISPLAY_KEYS + SUPPORT_KEYS
EXTRA_LABELS = {key: label for key, label, _max in EXTRA_FIELDS}

SUPPORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("yes", "Yes — vendor / contract"),
    ("internal", "Internal — we hold it"),
    ("no", "No contract"),
    ("custom", "Custom — describe below"),
)
SUPPORT_CUSTOM_MAX = 120
SUPPORT_LEAD_CHOICES = (7, 14, 30)
SUPPORT_LEAD_DEFAULT = 14


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _normalize_support(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    kind = str(raw.get("support") or "").strip().lower()
    if kind in {key for key, _label in SUPPORT_CHOICES}:
        out["support"] = kind
    if kind == "custom":
        note = " ".join(str(raw.get("support_custom") or "").split())[:SUPPORT_CUSTOM_MAX]
        if note:
            out["support_custom"] = note
    for key in ("support_from", "support_to"):
        parsed = _iso_date(raw.get(key))
        if parsed is not None:
            out[key] = parsed.isoformat()
    if out:
        try:
            lead = int(str(raw.get("support_lead_days") or "").strip())
        except ValueError:
            lead = SUPPORT_LEAD_DEFAULT
        out["support_lead_days"] = str(lead if lead in SUPPORT_LEAD_CHOICES else SUPPORT_LEAD_DEFAULT)
    return out


def normalize_extras(raw: Any) -> dict[str, str]:
    """Known keys only, stripped and length-capped. Empty values are dropped."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, _label, limit in EXTRA_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()[:limit]
        if text:
            out[key] = text
    out.update(_normalize_support(raw))
    return out


def form_extras(asset: Any = None) -> dict[str, str]:
    """Every key present (empty string when unset) so templates can read form.extras.site directly."""
    stored = normalize_extras(getattr(asset, "extras", None)) if asset is not None else {}
    return {key: stored.get(key, "") for key in EXTRA_KEYS}


def extras_rows(asset: Any) -> list[tuple[str, str, str]]:
    """(key, label, value) for filled fields, in form order. Used by Who to call and mail bodies."""
    stored = normalize_extras(getattr(asset, "extras", None))
    return [(key, EXTRA_LABELS[key], stored[key]) for key in DISPLAY_KEYS if key in stored]


def support_status(asset: Any, today: date | None = None) -> dict[str, Any]:
    """In support / Expiring / Out of support / Unknown from the stored support block. Read-only."""
    stored = normalize_extras(getattr(asset, "extras", None) if asset is not None else None)
    today = today or date.today()
    kind = stored.get("support", "")
    start = _iso_date(stored.get("support_from"))
    end = _iso_date(stored.get("support_to"))
    lead = int(stored.get("support_lead_days") or SUPPORT_LEAD_DEFAULT)
    days_left = (end - today).days if end else None

    def _out(state: str, label: str, tone: str, detail: str, call_note: str) -> dict[str, Any]:
        return {
            "state": state,
            "label": label,
            "tone": tone,
            "detail": detail,
            "call_note": call_note,
            "kind": kind,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "lead_days": lead,
            "days_left": days_left,
            "warn": state in {"expiring", "expired"},
            "custom_note": custom,
        }

    out_note = "Out of support — vendor may not take a ticket"
    custom = stored.get("support_custom", "") if kind == "custom" else ""
    if kind == "no":
        return _out("expired", "Out of support", "crit", "No support contract", out_note)
    if start is None and end is None and kind != "custom":
        return _out("unknown", "Support unknown", "grey", "Support dates not filled", "Unknown — support dates not filled")
    if end is not None and today > end:
        return _out("expired", "Out of support", "crit", f"Support ended {end.isoformat()}", out_note)
    if start is not None and today < start:
        return _out("expired", "Out of support", "crit", f"Support starts {start.isoformat()}", out_note)
    holder = {"internal": "Internal support", "custom": f"Custom support ({custom or 'no note'})"}.get(kind, "Support")
    if end is not None and days_left is not None and days_left <= lead:
        left = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        detail = f"{holder} ends {end.isoformat()} ({left})"
        return _out("expiring", "Support expiring", "warn", detail, f"Expiring — {detail}")
    detail = f"{holder} until {end.isoformat()}" if end else f"{holder}, no end date"
    if kind == "custom":
        return _out("in", "Custom", "ok", detail, f"Custom support — {custom or 'no note'}" + (f" until {end.isoformat()}" if end else ""))
    return _out("in", "In support", "ok", detail, f"In support — {detail}")


def normalize_playrule_ids(raw: Any) -> list[int]:
    """Positive unique ints in posted order. Junk is ignored."""
    if raw is None:
        return []
    if isinstance(raw, (str, int)):
        raw = [raw]
    out: list[int] = []
    for item in raw if isinstance(raw, Iterable) else []:
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


def asset_playrule_ids(asset: Any) -> list[int]:
    return normalize_playrule_ids(getattr(asset, "playrule_ids", None))


def known_playrule_ids(db: Any, raw: Any) -> list[int]:
    """Posted ids that still exist as playrules (enabled or not), in posted order."""
    from app.models import Playrule

    wanted = normalize_playrule_ids(raw)
    if not wanted:
        return []
    found = {int(pk) for (pk,) in db.query(Playrule.id).filter(Playrule.id.in_(wanted)).all()}
    return [pk for pk in wanted if pk in found]


def extras_from_form(present: str, **values: str) -> dict[str, str] | None:
    """Posted extra_* fields. None when the form did not carry the block (keep what is stored)."""
    if not str(present or "").strip():
        return None
    if str(values.get("support") or "").strip().lower() == "custom" and not str(values.get("support_custom") or "").strip():
        raise ValueError("Under support = Custom needs a short note (e.g. weekdays 8–16)")
    return normalize_extras({key: values.get(key, "") for key in EXTRA_KEYS})


def playrule_ids_from_form(present: str, posted: list[str] | None, add: str = "") -> list[int] | None:
    """Posted Client playrule list (hidden playrule_ids rows, then the dropdown pick if JS did not
    move it into the list). None when the block was absent; [] when every row was removed."""
    if not str(present or "").strip():
        return None
    return normalize_playrule_ids([*(posted or []), *([add] if str(add or "").strip() else [])])
