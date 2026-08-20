"""Strip secrets before any LLM prompt. RCA never needs credentials."""

from __future__ import annotations

import re
from typing import Any

SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "ssh",
    "snmp",
)
REDACTED = "[REDACTED]"
BEARER = re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]+")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _secret_key(str(key)):
                out[key] = REDACTED
            else:
                out[key] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return BEARER.sub(r"\1[REDACTED]", value)
    return value


def _secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in SECRET_KEY_PARTS)
