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
# Same four rungs as the asset form / incident cubes. Role targets mail the owner email
# (oncall mails whoever is on the roster now). An address is mailed as written.
POLICY_LEVEL_HINTS: dict[int, tuple[int, str]] = {
    1: (0, "team"),
    2: (15, "team-lead"),
    3: (30, "engineer"),
    4: (60, "manager"),
}
DEFAULT_POLICY_STEPS: list[dict[str, Any]] = [
    {"after_minutes": minutes, "target": target, "channel": "email", "level": level}
    for level, (minutes, target) in POLICY_LEVEL_HINTS.items()
]
VANILLA_THREE_DEFAULT = (
    (0, "team"),
    (15, "team-lead"),
    (30, "engineer"),
)
MAX_MINUTES = 10080


def ladder_label(level: int) -> str:
    for number, label in LADDER_LEVELS:
        if number == level:
            return label
    return f"L{level}"


def _step_level(step: dict[str, Any], index: int) -> int:
    raw = step.get("level")
    try:
        level = int(raw)
    except (TypeError, ValueError):
        level = 0
    if level in (1, 2, 3, 4):
        return level
    return 4 if index >= 3 else index + 1


def policy_step_signature(steps: list[dict[str, Any]] | None) -> tuple[tuple[int, str], ...]:
    rows: list[tuple[int, str]] = []
    for step in steps or []:
        try:
            after = int(step.get("after_minutes") or 0)
        except (TypeError, ValueError):
            after = 0
        rows.append((after, str(step.get("target") or "").strip().lower()))
    return tuple(rows)


def is_vanilla_three_default(steps: list[dict[str, Any]] | None) -> bool:
    """Unedited Default warning from before L4 existed — safe to add Manager."""
    return policy_step_signature(steps) == VANILLA_THREE_DEFAULT


def with_policy_levels(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Copy steps and stamp L1–L4 when the stored JSON has no level yet."""
    out: list[dict[str, Any]] = []
    for index, step in enumerate(steps or []):
        row = dict(step)
        row["channel"] = "email"
        row["level"] = _step_level(row, index)
        out.append(row)
    return out


def policy_ladder_slots(steps: list[dict[str, Any]] | None, *, create: bool = False) -> list[dict[str, Any]]:
    """Four L1–L4 rows for Create / Edit policy. Missing levels are off (create fills the seed)."""
    by_level: dict[int, dict[str, Any]] = {}
    for index, step in enumerate(steps or []):
        level = _step_level(step, index)
        by_level.setdefault(level, step)
    slots: list[dict[str, Any]] = []
    for level, label in LADDER_LEVELS:
        hint_minutes, hint_target = POLICY_LEVEL_HINTS[level]
        row = by_level.get(level)
        if create and not steps:
            row = {"after_minutes": hint_minutes, "target": hint_target, "level": level}
        on = row is not None
        minutes = hint_minutes if row is None else int(row.get("after_minutes") or 0)
        target = hint_target if row is None else str(row.get("target") or "").strip() or hint_target
        slots.append(
            {
                "level": level,
                "label": label,
                "on": on,
                "minutes": minutes,
                "target": target,
            }
        )
    return slots


def policy_steps_from_levels(
    ons: list[str] | list[Any],
    minutes: list[str] | list[Any],
    targets: list[str] | list[Any],
) -> list[dict[str, Any]]:
    """Posted L1–L4. OFF or blank recipient is omitted — that cube stays gray."""
    out: list[dict[str, Any]] = []
    for index, (level, _label) in enumerate(LADDER_LEVELS):
        on = ons[index] if index < len(ons) else ""
        if not str(on or "").strip():
            continue
        target = str(targets[index] if index < len(targets) else "").strip()
        if not target:
            continue
        out.append(
            {
                "after_minutes": _minutes(minutes[index] if index < len(minutes) else 0),
                "target": target[:255],
                "channel": "email",
                "level": level,
            }
        )
    return out


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


def filled_ladder_levels(db: Session, asset: Asset | None) -> list[dict[str, Any]]:
    """Enabled levels that have at least one email, in level order.

    Empty means this asset uses the Default ladder (or the matched rule's ladder).
    Mail steps are one entry per address, in this same order.
    """
    if asset is None or not getattr(asset, "id", None):
        return []
    rows = (
        db.query(AssetLadderStep)
        .filter(AssetLadderStep.asset_id == asset.id, AssetLadderStep.enabled.is_(True))
        .order_by(AssetLadderStep.level.asc(), AssetLadderStep.id.asc())
        .all()
    )
    levels: list[dict[str, Any]] = []
    for row in rows:
        emails: list[str] = []
        for raw in row.emails or []:
            address = _valid_email(str(raw))
            if address:
                emails.append(address)
        if not emails:
            continue
        levels.append(
            {
                "level": int(row.level),
                "label": ladder_label(int(row.level)),
                "after_minutes": int(row.after_minutes or 0),
                "emails": emails,
            }
        )
    return levels


def active_mail_steps(db: Session, asset: Asset | None) -> list[dict[str, Any]] | None:
    """Email steps for this asset, or None when the ladder is empty (use Default)."""
    steps: list[dict[str, Any]] = []
    for level in filled_ladder_levels(db, asset):
        minutes = int(level["after_minutes"])
        for email in level["emails"]:
            steps.append({"after_minutes": minutes, "target": email, "channel": "email"})
    return steps or None
