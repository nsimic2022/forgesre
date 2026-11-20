"""On-call week for the new UI: every roster slot, who is on call now, and the people to pick from.

Writes go through app/dc_noc.py (add_oncall_slot, update_oncall_slot, take_duty), the same
validation everywhere on-call is edited. Times are appliance-local minutes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.dc_noc import WEEKDAYS, current_oncall, oncall_directory, oncall_slot_view
from app.models import OncallSlot, User
from app.security import can
from app.settings import settings


def oncall_week(db: Session, user: User) -> dict[str, Any]:
    """Roster grouped by weekday (Monday = 0) plus the person covering this minute."""
    rows = db.query(OncallSlot).order_by(OncallSlot.weekday, OncallSlot.start_minute, OncallSlot.id).all()
    slots = []
    for row in rows:
        view = oncall_slot_view(row)
        view["start_minute"] = int(row.start_minute)
        view["end_minute"] = int(row.end_minute)
        slots.append(view)
    editable = can(user, "write_play")
    return {
        "weekdays": list(WEEKDAYS),
        "slots": slots,
        "now": current_oncall(db),
        "timezone": getattr(settings, "timezone", "") or "",
        "people": oncall_directory(db) if editable else [],
        "can_edit": editable,
    }
