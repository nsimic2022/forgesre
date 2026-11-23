"""Appliance checks: TLS certificate expiry, ForgeSRE's own backup age, and heartbeats.

Run by the ``health_checks`` job (the leader queues it every 15 minutes). Each check writes its
latest result to check_results and fires or resolves an alert through ``ingest_alertmanager``,
so routing rules, noise filters, maintenance and escalation work as for any Prometheus alert.

- TLS: every ``checks.tls_targets`` entry (``host``, ``host:port`` or ``https://host[:port]``);
  warning under ``checks.tls_warn_days`` (14), critical under ``checks.tls_critical_days`` (3),
  error when the handshake fails. 10 s timeout per target.
- Backup: the newest ForgeSRE archive must be younger than ``checks.backup_max_age_hours`` (36).
- Heartbeats: a job calls ``POST /api/v1/heartbeats/{name}`` after each success; no ping within
  its ``every_hours`` (plus 10 % grace) is critical. Used to prove other backups really ran.
- Lifecycle: support / warranty end, vendor end of life and licence expiry per asset
  (``app.lifecycle``), alert ``AssetLifecycleExpiring``.
"""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import CheckResult, Heartbeat, utcnow
from app.settings import settings

TIMEOUT = 10.0
ALERTS = {"tls": "TlsCertificateExpiring", "backup": "ForgeSreBackupMissing", "heartbeat": "HeartbeatMissing"}


def _cfg() -> dict[str, Any]:
    return (settings.yaml or {}).get("checks") or {}


def tls_targets() -> list[tuple[str, int]]:
    raw = _cfg().get("tls_targets") or []
    items = raw if isinstance(raw, list) else str(raw).replace(";", ",").split(",")
    out: list[tuple[str, int]] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        parsed = urlparse(text if "://" in text else f"https://{text}")
        if parsed.hostname:
            out.append((parsed.hostname, parsed.port or 443))
    return out


def cert_days_left(host: str, port: int = 443, timeout: float = TIMEOUT) -> float:
    """Days until the served certificate expires. Verification is off on purpose: an internal CA
    or a self-signed cert still has an expiry date worth watching. Raises OSError / ssl.SSLError."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    from cryptography import x509

    cert = x509.load_der_x509_certificate(der)
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=timezone.utc)
    return (not_after - datetime.now(timezone.utc)).total_seconds() / 86400


def _save(db: Session, kind: str, target: str, status: str, value: float | None, detail: str) -> CheckResult:
    row = db.query(CheckResult).filter_by(kind=kind, target=target).first() or CheckResult(kind=kind, target=target)
    row.status, row.value, row.detail, row.checked_at = status, value, detail[:500], utcnow()
    db.add(row)
    return row


def _alert(kind: str, target: str, status: str, detail: str, asset: str = "") -> dict[str, Any]:
    severity = {"warning": "warning", "critical": "critical", "error": "high"}.get(status, "warning")
    return {
        "status": "resolved" if status == "ok" else "firing",
        "labels": {"alertname": ALERTS[kind], "asset": asset or target, "severity": severity, "job": "forgesre-checks",
                   "check": kind, "target": target},
        "annotations": {"summary": f"{ALERTS[kind]}: {target}", "description": detail},
        "startsAt": utcnow().isoformat(),
    }


def check_tls(db: Session, probe=cert_days_left) -> list[dict[str, Any]]:
    warn = float(_cfg().get("tls_warn_days") or 14)
    crit = float(_cfg().get("tls_critical_days") or 3)
    alerts = []
    for host, port in tls_targets():
        target = f"{host}:{port}"
        try:
            days = probe(host, port)
        except (OSError, ssl.SSLError, ValueError) as exc:
            status, days, detail = "error", None, f"TLS handshake failed: {type(exc).__name__}: {exc}"[:300]
        else:
            status = "critical" if days < crit else "warning" if days < warn else "ok"
            detail = f"certificate expires in {days:.1f} days" if days >= 0 else f"certificate expired {-days:.1f} days ago"
        _save(db, "tls", target, status, days, detail)
        alerts.append(_alert("tls", target, status, detail, host))
    return alerts


def check_backup(db: Session, archives=None) -> list[dict[str, Any]]:
    from app.backup import list_archives

    limit = float(_cfg().get("backup_max_age_hours") or 36)
    rows = archives if archives is not None else list_archives()
    if rows:
        newest = max(datetime.fromisoformat(r["mtime"]) for r in rows)
        newest = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        age = (utcnow() - newest).total_seconds() / 3600
        status = "critical" if age > limit else "ok"
        detail = f"newest ForgeSRE backup is {age:.1f} h old (limit {limit:g} h)"
    else:
        age, status, detail = None, "critical", "no ForgeSRE backup on disk; run ./forgesre backup or schedule one"
    _save(db, "backup", "forgesre", status, age, detail)
    return [_alert("backup", "forgesre", status, detail, "forgesre")]


def check_heartbeats(db: Session) -> list[dict[str, Any]]:
    alerts = []
    now = utcnow()
    for hb in db.query(Heartbeat).all():
        allowed = timedelta(hours=float(hb.every_hours or 24) * 1.1)
        last = hb.last_at if hb.last_at is None or hb.last_at.tzinfo else hb.last_at.replace(tzinfo=timezone.utc)
        late = last is None or now - last > allowed
        age = None if last is None else (now - last).total_seconds() / 3600
        detail = (f"no ping for {age:.1f} h (expected every {hb.every_hours:g} h)" if late and age is not None
                  else "never pinged" if late else f"last ping {age:.1f} h ago")
        status = "critical" if late else "ok"
        _save(db, "heartbeat", hb.name, status, age, detail)
        alerts.append(_alert("heartbeat", hb.name, status, detail, hb.asset_ref))
    return alerts


def run_checks(db: Session, probe=cert_days_left, archives=None) -> dict[str, int]:
    """All checks, then one Alertmanager-shaped payload so incidents open and resolve normally."""
    from app.services import ingest_alertmanager

    from app.lifecycle import check_lifecycle

    alerts = check_tls(db, probe) + check_backup(db, archives) + check_heartbeats(db) + check_lifecycle(db)
    db.commit()
    if alerts:
        ingest_alertmanager(db, {"status": "firing", "alerts": alerts}, source="prometheus")
        db.commit()
    return {"checks": len(alerts), "failing": sum(1 for a in alerts if a["status"] == "firing")}


def ping(db: Session, name: str, every_hours: float | None = None, asset: str = "", note: str = "") -> Heartbeat:
    """Record one heartbeat; the first ping creates it. Name: letters, digits, dot, dash, underscore."""
    clean = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "._-")[:100]
    if not clean:
        raise ValueError("heartbeat name is required (letters, digits, . _ -)")
    row = db.query(Heartbeat).filter_by(name=clean).first() or Heartbeat(name=clean)
    if every_hours is not None:
        if not 0.05 <= float(every_hours) <= 24 * 31:
            raise ValueError("every_hours must be between 0.05 and 744")
        row.every_hours = float(every_hours)
    if asset:
        row.asset_ref = str(asset)[:255]
    row.last_at, row.last_note = utcnow(), str(note or "")[:255]
    db.add(row)
    db.commit()
    return row


def results(db: Session) -> dict[str, Any]:
    rows = db.query(CheckResult).order_by(CheckResult.kind, CheckResult.target).all()
    return {
        "results": [{"kind": r.kind, "target": r.target, "status": r.status, "value": r.value, "detail": r.detail,
                     "checked_at": r.checked_at.isoformat() if r.checked_at else None} for r in rows],
        "heartbeats": [{"name": h.name, "every_hours": h.every_hours, "asset": h.asset_ref, "note": h.last_note,
                        "last_at": h.last_at.isoformat() if h.last_at else None} for h in db.query(Heartbeat).order_by(Heartbeat.name)],
        "tls_targets": [f"{h}:{p}" for h, p in tls_targets()],
    }


def doctor_check(db: Session) -> dict[str, Any]:
    rows = db.query(CheckResult).all()
    bad = [r for r in rows if r.status != "ok"]
    if not rows:
        return {"status": "disabled", "why": "no check has run yet"}
    if bad:
        # warn, not error: an expiring certificate elsewhere is an incident, not a broken appliance.
        return {"status": "warn", "why": "; ".join(f"{r.kind} {r.target}: {r.detail}" for r in bad[:3])[:300]}
    return {"status": "ok", "why": f"{len(rows)} checks ok"}
