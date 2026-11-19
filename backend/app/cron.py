"""Five-field cron expressions for scheduled reports: minute hour day-of-month month day-of-week.

Supports ``*``, numbers, ranges ``1-5``, lists ``1,15`` and steps ``*/15`` / ``8-18/2``.
Day of week is 0-7 (0 and 7 = Sunday) or mon..sun; month is 1-12 or jan..dec. Like cron,
when both day fields are restricted a day matches if either does. Times are local to the
appliance time zone. No dependency: reports only need "next run after now".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_NAMES = {
    4: {n: i for i, n in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)},
    5: {n: i for i, n in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))},
}
_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELDS = ("minute", "hour", "day of month", "month", "day of week")


def _value(text: str, index: int) -> int:
    key = text.strip().lower()
    names = _NAMES.get(index + 1, {})
    if key in names:
        return names[key]
    if not key.isdigit():
        raise ValueError(f"cron {_FIELDS[index]}: {text!r} is not a number")
    return int(key)


def _field(text: str, index: int) -> set[int]:
    low, high = _LIMITS[index]
    out: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text.isdigit() and int(step_text) > 0 else (1 if not step_text else 0)
        if step == 0:
            raise ValueError(f"cron {_FIELDS[index]}: bad step in {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _value(a, index), _value(b, index)
        else:
            start = end = _value(base, index)
            if step_text:
                end = high
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise ValueError(f"cron {_FIELDS[index]}: {part!r} is outside {low}-{high}")
        out.update(range(start, end + 1, step))
    if index == 4 and 7 in out:
        out.discard(7)
        out.add(0)
    return out


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Raises ValueError with a readable reason. Returns the five sets plus 'restricted' flags for the day fields."""
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("cron needs five fields: minute hour day-of-month month day-of-week (e.g. 0 7 * * 1)")
    sets = [_field(part, i) for i, part in enumerate(parts)]
    return sets[0], sets[1], sets[2], sets[3], sets[4], parts[2] != "*", parts[4] != "*"


def next_after(expr: str, after: datetime, tz: str = "UTC") -> datetime:
    """First matching minute strictly after ``after`` (UTC), searched up to 4 years ahead."""
    minutes, hours, mdays, months, wdays, dom_set, dow_set = parse(expr)
    zone = ZoneInfo(tz or "UTC")
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    local = after.astimezone(zone).replace(second=0, microsecond=0)
    day = local.replace(hour=0, minute=0)
    for _ in range(366 * 4):
        cron_dow = (day.weekday() + 1) % 7  # Python Monday=0 -> cron Sunday=0
        dom_ok, dow_ok = day.day in mdays, cron_dow in wdays
        day_ok = (dom_ok or dow_ok) if (dom_set and dow_set) else (dom_ok and dow_ok)
        if day.month in months and day_ok:
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate > local:
                        return candidate.astimezone(timezone.utc)
        day = (day + timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError(f"cron {expr!r} never fires")


def describe(expr: str) -> str:
    """Short label for lists, e.g. 'cron 0 7 * * 1'."""
    return f"cron {' '.join((expr or '').split())}"
