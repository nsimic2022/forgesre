"""Security overview for Settings › Security and ./forgesre security: platform checks from Core
(HTTPS, webhook token, Admin accounts, unused accounts, data that leaves the server) plus the last
host report written by the CLI (app.host_security). Read-only: nothing here changes a setting."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.host_security import check
from app.models import AuditLog, User, utcnow
from app.settings import settings

# An account nobody used for this long is a door nobody watches.
UNUSED_DAYS = 90


def _on(value: Any) -> bool:
    return str(value or "").strip().lower() in {"on", "only", "1", "true", "yes"}


def platform_checks(db: Session) -> list[dict[str, str]]:
    """Checks Core can answer itself, without the host."""
    out = []
    https = str(os.environ.get("FORGESRE_HTTPS") or "off").lower()
    if _on(https):
        cookie = settings.cookie_secure
        out.append(check("https", "HTTPS", "ok" if cookie else "warn",
                         "The UI is served over HTTPS" + ("; session cookies are Secure." if cookie else ", but session cookies are not marked Secure."),
                         "" if cookie else "Set FORGESRE_COOKIE_SECURE=true in .env, then ./forgesre restart core"))
    else:
        out.append(check("https", "HTTPS", "warn", "The UI is plain HTTP: passwords and sessions travel unencrypted on the network.",
                         "./forgesre https on (own certificate or one from your CA); docs/install-config.md"))
    out.append(check("webhook_token", "Webhook token", "ok" if settings.webhook_token else "bad",
                     "Alerts are accepted only with the token." if settings.webhook_token else "ALERTMANAGER_WEBHOOK_TOKEN is empty: anyone on the network can post alerts.",
                     "" if settings.webhook_token else "./forgesre settings set-secret ALERTMANAGER_WEBHOOK_TOKEN, then ./forgesre restart"))

    users = db.query(User).filter(User.is_active.is_(True)).all()
    admins = [u.email for u in users if u.role in {"admin", "super_admin"}]
    out.append(check("admins", "Admin accounts", "warn" if len(admins) > 3 else "ok",
                     f"{len(admins)} active Admin account(s): {', '.join(sorted(admins)[:6])}{' …' if len(admins) > 6 else ''}.",
                     "Give Engineer to people who do not change settings, users or backups." if len(admins) > 3 else ""))
    last = dict(db.query(AuditLog.actor, func.max(AuditLog.at)).filter(AuditLog.action == "login").group_by(AuditLog.actor).all())
    cutoff = utcnow() - timedelta(days=UNUSED_DAYS)

    def stale(u: User) -> bool:
        seen = last.get(u.email)
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        created = u.created_at if (u.created_at is None or u.created_at.tzinfo) else u.created_at.replace(tzinfo=timezone.utc)
        return (seen or created or utcnow()) < cutoff

    unused = sorted(u.email for u in users if stale(u))
    out.append(check("unused", "Unused accounts", "warn" if unused else "ok",
                     (f"No sign-in for {UNUSED_DAYS}+ days: {', '.join(unused[:8])}{' …' if len(unused) > 8 else ''}." if unused
                      else f"Every active account signed in within {UNUSED_DAYS} days."),
                     "Settings › Users: turn off accounts that are no longer needed." if unused else ""))

    leaves = []
    if settings.cloud_enabled:
        leaves.append("cloud LLM (ai.cloud)")
    from app.mcp_server import enabled as mcp_enabled

    if mcp_enabled():
        leaves.append("MCP access for outside AI tools")
    out.append(check("data_out", "Data leaving the server", "info" if leaves else "ok",
                     ("Turned on: " + ", ".join(leaves) + ".") if leaves else "Nothing sends incident data outside: the LLM is local, cloud and MCP are off.",
                     "Turn off what is not needed in Settings › ForgeAI / Integrations." if leaves else ""))
    return out


def host_report(max_age_days: int = 7) -> dict[str, Any] | None:
    """The last host report written by ./forgesre security (or doctor), with ``stale`` when old."""
    base = Path(os.environ.get("FORGESRE_GENERATED_DIR") or "data/generated")
    try:
        report = json.loads((base / "security-host.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        at = datetime.fromisoformat(str(report.get("checked_at")))
        report["stale"] = (datetime.now(timezone.utc) - at) > timedelta(days=max_age_days)
    except ValueError:
        report["stale"] = True
    return report


def summary(checks: list[dict[str, str]]) -> dict[str, int]:
    """How many findings per status."""
    out = {"bad": 0, "warn": 0, "ok": 0, "info": 0, "unknown": 0}
    for c in checks:
        out[c.get("status", "unknown")] = out.get(c.get("status", "unknown"), 0) + 1
    return out


def security_overview(db: Session) -> dict[str, Any]:
    """Platform checks plus the last host report, for the API and the CLI."""
    platform = platform_checks(db)
    host = host_report()
    every = platform + list((host or {}).get("checks") or [])
    return {"platform": platform, "host": host, "summary": summary(every)}
