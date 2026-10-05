"""V0.8 NOC fields on an asset (assets.extras JSON) and the per-asset playrule preference (assets.playrule_ids).

Local Core data only — never written to NetBox. Playrule ids are a preference for which enabled
playrule handles an alertname on this host; they never create or change Prometheus rules.
"""

from __future__ import annotations

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
EXTRA_KEYS = tuple(key for key, _label, _max in EXTRA_FIELDS)
EXTRA_LABELS = {key: label for key, label, _max in EXTRA_FIELDS}


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
    return out


def form_extras(asset: Any = None) -> dict[str, str]:
    """Every key present (empty string when unset) so templates can read form.extras.site directly."""
    stored = normalize_extras(getattr(asset, "extras", None)) if asset is not None else {}
    return {key: stored.get(key, "") for key in EXTRA_KEYS}


def extras_rows(asset: Any) -> list[tuple[str, str, str]]:
    """(key, label, value) for filled fields, in form order. Used by Who to call and mail bodies."""
    stored = normalize_extras(getattr(asset, "extras", None))
    return [(key, EXTRA_LABELS[key], stored[key]) for key in EXTRA_KEYS if key in stored]


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
    return normalize_extras({key: values.get(key, "") for key in EXTRA_KEYS})


def playrule_ids_from_form(present: str, posted: list[str] | None) -> list[int] | None:
    """Posted Client playrule checkboxes. None when the block was absent; [] when all are unticked."""
    if not str(present or "").strip():
        return None
    return normalize_playrule_ids(posted or [])
