"""One-click acknowledge from an escalation mail or chat message.

The link carries a signed token (incident id + recipient, 24 h). Opening it shows a small page
with an Acknowledge button. Mail scanners (Outlook Safe Links and similar) open every link, so
a GET must never acknowledge on its own; only the POST from that page does. The ack is recorded
as the recipient the link was sent to.

Links are only added when ``system.public_url`` is set, because the appliance does not know
the address people reach it by.
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.settings import settings

MAX_AGE = 24 * 3600
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="forgesre-ack")


def public_url() -> str:
    return str(((settings.yaml or {}).get("system") or {}).get("public_url") or "").strip().rstrip("/")


def make_token(incident_id: int, recipient: str) -> str:
    return _serializer.dumps({"i": int(incident_id), "r": str(recipient or "")[:255]})


def ack_url(incident_id: int, recipient: str) -> str:
    """Full link, or "" when no public URL is configured."""
    base = public_url()
    return f"{base}/ack/{make_token(incident_id, recipient)}" if base else ""


def read_token(token: str, max_age: int = MAX_AGE) -> dict[str, Any]:
    """{"incident_id", "recipient"}; raises ValueError("expired" | "invalid")."""
    try:
        data = _serializer.loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise ValueError("expired") from exc
    except BadSignature as exc:
        raise ValueError("invalid") from exc
    return {"incident_id": int(data.get("i") or 0), "recipient": str(data.get("r") or "")}
