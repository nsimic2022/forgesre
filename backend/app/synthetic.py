"""Synthetic checks: probe services from the appliance the way a user would.

Each check is a row in ``synthetic_checks``. Prometheus job ``forgesre-synthetic`` reads them
through HTTP SD (``/api/v1/sd/synthetic``) and asks blackbox_exporter (127.0.0.1:9115) to
probe each target every 60 s with the module for its kind. Rules ``SyntheticCheckFailed``
(critical checks) and ``SyntheticCheckDegraded`` (warning checks) fire on probe_success == 0.
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import SyntheticCheck

JOB = "forgesre-synthetic"
# kind → blackbox module in monitoring/blackbox.yml
MODULES = {"http": "http_2xx", "tcp": "tcp_connect", "dns": "dns_answer", "icmp": "icmp"}
SEVERITIES = ("critical", "warning")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_HOSTPORT = re.compile(r"^[A-Za-z0-9.\-\[\]:]+:\d{1,5}$")
_HOST = re.compile(r"^[A-Za-z0-9.\-:]+$")


def clean_target(kind: str, target: str) -> str:
    """Validate the target for its kind; raises ValueError with a sentence an operator can act on."""
    text = str(target or "").strip()
    if kind == "http":
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HTTP target must be a URL such as https://erp.dc.local/health")
        return text[:500]
    if kind == "tcp":
        if not _HOSTPORT.match(text):
            raise ValueError("TCP target must be host:port, e.g. db01:5432")
        return text
    if kind in {"dns", "icmp"}:
        host = text.split(":")[0] if kind == "icmp" else text
        if not _HOST.match(host):
            raise ValueError("DNS target is the DNS server (ip or ip:53); ping target is a host or IP")
        return host if kind == "icmp" else (text if ":" in text else f"{text}:53")
    raise ValueError("kind must be http, tcp, dns or icmp")


def as_dict(row: SyntheticCheck, state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or {}
    return {"id": row.id, "name": row.name, "kind": row.kind, "target": row.target, "asset": row.asset_ref,
            "severity": row.severity, "enabled": bool(row.enabled), "module": MODULES.get(row.kind, ""),
            "up": state.get("up"), "seconds": state.get("seconds")}


def save(db: Session, data: dict[str, Any], actor: str, check_id: int | None = None) -> SyntheticCheck:
    """Create or update one check after validating name, kind, target and severity."""
    name = str(data.get("name") or "").strip()
    kind = str(data.get("kind") or "http").strip().lower()
    if not _NAME.match(name):
        raise ValueError("name: letters, digits, dot, dash, underscore (max 100)")
    if kind not in MODULES:
        raise ValueError("kind must be http, tcp, dns or icmp")
    severity = str(data.get("severity") or "critical").strip().lower()
    if severity not in SEVERITIES:
        raise ValueError("severity must be critical or warning")
    target = clean_target(kind, str(data.get("target") or ""))
    row = db.get(SyntheticCheck, check_id) if check_id else None
    if check_id and row is None:
        raise LookupError("check not found")
    clash = db.query(SyntheticCheck).filter(SyntheticCheck.name == name).first()
    if clash is not None and clash is not row:
        raise ValueError(f"a check named {name} already exists")
    row = row or SyntheticCheck(created_by=actor)
    row.name, row.kind, row.target, row.severity = name, kind, target, severity
    row.asset_ref = str(data.get("asset") or "").strip()[:255]
    row.enabled = bool(data.get("enabled", True))
    db.add(row)
    db.commit()
    return row


def sd_targets(db: Session) -> list[dict[str, Any]]:
    """Prometheus HTTP SD: one target per enabled check, module and labels for the rules."""
    out = []
    for row in db.query(SyntheticCheck).filter(SyntheticCheck.enabled.is_(True)).order_by(SyntheticCheck.name):
        out.append({"targets": [row.target], "labels": {
            "module": MODULES[row.kind], "check": row.name, "kind": row.kind,
            "asset": row.asset_ref or row.name, "check_severity": row.severity}})
    return out


def overview(db: Session, vector_fn: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """All checks with the latest probe_success and duration from Prometheus (2 s timeout)."""
    if vector_fn is None:
        from app.dc_noc import query_prometheus_vector as vector_fn
    rows = db.query(SyntheticCheck).order_by(SyntheticCheck.name).all()
    state: dict[str, dict[str, Any]] = {}
    error = ""
    if rows:
        for key, expr in (("up", f'probe_success{{job="{JOB}"}}'), ("seconds", f'probe_duration_seconds{{job="{JOB}"}}')):
            got = vector_fn(expr)
            if got.get("error"):
                error = str(got["error"])
                break
            for series in got.get("series") or []:
                name = str((series.get("labels") or {}).get("check") or "")
                if name:
                    state.setdefault(name, {})[key] = round(float(series["value"]), 3)
    return {"checks": [as_dict(r, state.get(r.name)) for r in rows], "error": error}
