"""Reading a scheduled-report form (name, recipients, schedule, assets) for the reports API."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session


def _collect_report_emails(db: Session, picked: list[str] | str, extra: str, actor: str) -> list[str]:
    from app.services import remember_mail_contact

    raw: list[str] = []
    if isinstance(picked, str):
        raw.append(picked)
    else:
        raw.extend(picked or [])
    blob = (extra or "").replace(";", ",").replace("\n", ",")
    raw.extend(part.strip() for part in blob.split(","))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        contact = remember_mail_contact(db, item, actor=actor)
        if contact is None:
            continue
        key = contact.email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(contact.email)
    return out



def parse_scheduled_report_form(
    db: Session,
    *,
    name: str,
    to_email: list[str] | str,
    new_email: str,
    interval_hours: int,
    asset_id: list[str],
    actor: str,
    extra_emails: str = "",
    schedule: str = "",
    every_n: str = "",
    every_unit: str = "minutes",
    weekday: list[str] | None = None,
    cron: str = "",
) -> dict:
    """Name, recipients, schedule, asset ids. SMTP path unchanged. Legacy interval_hours still works."""
    from app.services import build_schedule

    try:
        built = build_schedule(
            schedule=schedule,
            every_n=every_n,
            every_unit=every_unit,
            weekdays=list(weekday or []),
            interval_hours=int(interval_hours or 0),
            cron=cron,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ids = [str(item).strip() for item in (asset_id or []) if str(item).strip()]
    emails = _collect_report_emails(db, to_email, (extra_emails or "").strip() or (new_email or "").strip(), actor)
    if not emails:
        raise HTTPException(status_code=400, detail="Pick a saved address or enter a new email")
    return {
        "name": (name or "").strip() or "performance",
        "emails": emails,
        "email": emails[0],
        "hours": int(built["interval_hours"]),
        "ids": ids,
        "schedule": {
            "repeat": bool(built["repeat"]),
            "every_minutes": int(built["every_minutes"]),
            "weekdays": list(built["weekdays"]),
            "preset": str(built["preset"]),
            **({"cron": built["cron"]} if built.get("cron") else {}),
        },
    }
