"""Alarm noise at intake: alarm storms and flapping alerts.

Both keep the incident visible (status OPEN) but skip its own mail ladder, so the on-call person
gets one page for a storm instead of dozens, and one page for an alert that keeps coming back.
Settings (group Alarms): ``alerts.storm.*`` and ``alerts.flapping.*``. Parent-down suppression
lives in app.dc_noc and runs before these.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import Incident, utcnow

STORM_FINGERPRINT = "forgesre:alarm-storm"
DEFAULTS: dict[str, dict[str, Any]] = {
    "storm": {"enabled": True, "threshold": 20, "window_minutes": 5},
    "flapping": {"enabled": True, "threshold": 3, "window_minutes": 60},
}


def config(kind: str) -> dict[str, Any]:
    """``alerts.<kind>`` from config/forgesre.yml over the defaults, numbers clamped to sane ranges."""
    from app.settings import settings

    raw = ((settings.yaml.get("alerts") or {}).get(kind) or {}) if isinstance(settings.yaml, dict) else {}
    out = {**DEFAULTS[kind], **{k: v for k, v in raw.items() if k in DEFAULTS[kind]}}
    try:
        out["threshold"] = max(2, min(500, int(out["threshold"])))
        out["window_minutes"] = max(1, min(1440, int(out["window_minutes"])))
    except (TypeError, ValueError):
        out = dict(DEFAULTS[kind])
    out["enabled"] = bool(out["enabled"])
    return out


def active_storm(db: Session) -> Incident | None:
    from app.services import tracked_incident_statuses

    return (db.query(Incident).filter(Incident.fingerprint == STORM_FINGERPRINT, Incident.status.in_(tracked_incident_statuses()))
            .order_by(Incident.id.desc()).first())


def storm_for(db: Session, incident: Incident) -> tuple[Incident | None, bool]:
    """The storm this new incident belongs to, and whether that storm was opened just now.

    A storm is ``threshold`` or more new incidents within ``window_minutes``. The first time the
    count is reached, one CRITICAL incident "Alarm storm" opens (and pages); every new incident
    while it is active is linked to it (caused_by) and does not page on its own.
    """
    cfg = config("storm")
    if not cfg["enabled"] or incident.fingerprint == STORM_FINGERPRINT:
        return None, False
    storm = active_storm(db)
    if storm is not None:
        return storm, False
    since = utcnow() - timedelta(minutes=cfg["window_minutes"])
    count = (db.query(Incident).filter(Incident.started_at >= since, Incident.fingerprint != STORM_FINGERPRINT,
                                       Incident.caused_by_id.is_(None)).count())
    if count < cfg["threshold"]:
        return None, False
    from app.services import _insert_open_incident, append_timeline, next_incident_number

    storm = Incident(number=next_incident_number(db), title=f"Alarm storm: {count} new incidents in {cfg['window_minutes']} min",
                     severity="CRITICAL", status="OPEN", fingerprint=STORM_FINGERPRINT, source="forgesre",
                     summary="Many alarms at once. New incidents are linked here and do not page one by one; "
                             "look for a shared cause (power, uplink, storage, a change).",
                     alert_payload={"labels": {"alertname": "AlarmStorm", "severity": "critical"}}, timeline=[])
    inserted = _insert_open_incident(db, storm)
    if inserted is None:
        return active_storm(db), False
    append_timeline(inserted, "storm", "STORM", f"{count} new incidents in {cfg['window_minutes']} min")
    # incidents of the last window that are not linked yet join the storm
    for row in (db.query(Incident).filter(Incident.started_at >= since, Incident.id != inserted.id,
                                          Incident.fingerprint != STORM_FINGERPRINT, Incident.caused_by_id.is_(None)).all()):
        row.caused_by_id = inserted.id
    db.flush()
    from app.forgeai import queue_storm_summary

    queue_storm_summary(db, inserted, opened=True)
    return inserted, True


def join_storm(db: Session, incident: Incident, storm: Incident) -> None:
    from app.services import append_timeline

    incident.caused_by_id = storm.id
    append_timeline(incident, "storm", "STORM", f"Part of alarm storm {storm.number.split('_')[0]}; no own page")
    db.flush()  # the session does not autoflush: count this incident too
    linked = db.query(Incident).filter(Incident.caused_by_id == storm.id).count()
    storm.title = f"Alarm storm: {linked} incidents since {storm.started_at.strftime('%H:%M') if storm.started_at else 'now'}"
    from app.forgeai import queue_storm_summary

    queue_storm_summary(db, storm)


def flap_count(db: Session, incident: Incident) -> int:
    """How many incidents with this fingerprint started within the flapping window (this one
    included); 0 when flapping detection is off or the count is under the threshold."""
    cfg = config("flapping")
    if not cfg["enabled"] or not incident.fingerprint:
        return 0
    since = utcnow() - timedelta(minutes=cfg["window_minutes"])
    count = db.query(Incident).filter(Incident.fingerprint == incident.fingerprint, Incident.started_at >= since).count()
    return count if count >= cfg["threshold"] else 0
