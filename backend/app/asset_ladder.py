"""Per-asset mail ladder. Four fixed levels. No rows (or none filled) means the global Default ladder.

A filled level is enabled and has at least one email. Incidents on that host then use only
those steps — the matched rule still attaches the playbook, and does not add a second mail policy.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Asset, AssetLadderStep

LADDER_LEVELS: tuple[tuple[int, str], ...] = (
    (1, "L1 NOC"),
    (2, "L2 Shift"),
    (3, "L3 Engineer"),
    (4, "L4 Manager"),
)
MAX_MINUTES = 10080


def ladder_label(level: int) -> str:
    for number, label in LADDER_LEVELS:
        if number == level:
            return label
    return f"L{level}"


def _valid_email(value: str) -> str:
    text = (value or "").strip()
    local, _, domain = text.partition("@")
    if not local or "." not in domain or any(ch.isspace() for ch in text):
        return ""
    return text


def _minutes(raw: Any) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError:
        return 0
    if value < 0:
        return 0
    return min(value, MAX_MINUTES)


def ladder_steps_from_form(present: str, levels: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Posted Ladder column. None when the form did not include it (keep stored steps).

    A level that is off, or on with no address, is omitted — that is a delete.
    """
    if not str(present or "").strip():
        return None
    posted = list(levels or [])
    out: list[dict[str, Any]] = []
    for index, (level, _label) in enumerate(LADDER_LEVELS):
        row = posted[index] if index < len(posted) else {}
        if not str(row.get("on") or "").strip():
            continue
        emails_raw = row.get("emails") or []
        picks = row.get("picks") or []
        if isinstance(emails_raw, str):
            emails_raw = [emails_raw]
        if isinstance(picks, str):
            picks = [picks]
        emails: list[str] = []
        seen: set[str] = set()
        for i in range(max(len(emails_raw), len(picks))):
            typed = emails_raw[i] if i < len(emails_raw) else ""
            picked = picks[i] if i < len(picks) else ""
            address = _valid_email(str(picked or "")) or _valid_email(str(typed or ""))
            key = address.lower()
            if address and key not in seen:
                seen.add(key)
                emails.append(address)
        if not emails:
            continue
        out.append(
            {
                "level": level,
                "after_minutes": _minutes(row.get("minutes")),
                "emails": emails,
                "enabled": True,
            }
        )
    return out


def ladder_form_slots(asset: Asset | None) -> list[dict[str, Any]]:
    """Four fixed rows for Add / Edit. Missing levels are off."""
    saved: dict[int, AssetLadderStep] = {}
    if asset is not None:
        for row in list(getattr(asset, "ladder_steps", None) or []):
            emails = [str(item) for item in (row.emails or []) if _valid_email(str(item))]
            if row.enabled and emails:
                saved[int(row.level)] = row
    slots: list[dict[str, Any]] = []
    for level, label in LADDER_LEVELS:
        row = saved.get(level)
        emails = [str(item) for item in (row.emails or []) if _valid_email(str(item))] if row is not None else []
        slots.append(
            {
                "level": level,
                "label": label,
                "on": row is not None,
                "minutes": "" if row is None else int(row.after_minutes or 0),
                "emails": emails or [""],
            }
        )
    return slots


def replace_asset_ladder(db: Session, asset: Asset, steps: list[dict[str, Any]] | None) -> None:
    """Replace this asset's levels. None leaves them alone. [] clears them (Default ladder)."""
    if steps is None:
        return
    if asset.id is None:
        db.flush()
    for row in list(asset.ladder_steps or []):
        db.delete(row)
    db.flush()
    for step in steps:
        db.add(
            AssetLadderStep(
                asset_id=asset.id,
                level=int(step["level"]),
                after_minutes=int(step.get("after_minutes") or 0),
                emails=list(step.get("emails") or []),
                enabled=bool(step.get("enabled", True)),
            )
        )


def active_mail_steps(db: Session, asset: Asset | None) -> list[dict[str, Any]] | None:
    """Email steps for this asset, or None when the ladder is empty (use Default)."""
    if asset is None or not getattr(asset, "id", None):
        return None
    rows = (
        db.query(AssetLadderStep)
        .filter(AssetLadderStep.asset_id == asset.id, AssetLadderStep.enabled.is_(True))
        .order_by(AssetLadderStep.level.asc(), AssetLadderStep.id.asc())
        .all()
    )
    steps: list[dict[str, Any]] = []
    for row in rows:
        minutes = int(row.after_minutes or 0)
        for email in row.emails or []:
            address = _valid_email(str(email))
            if address:
                steps.append({"after_minutes": minutes, "target": address, "channel": "email"})
    return steps or None
