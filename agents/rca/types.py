"""RCA types owned by ForgeSRE. Engines consume these, not raw vendor payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass
class EvidenceItem:
    evidence_id: str
    type: str
    source: str
    timestamp: str
    asset_id: str = ""
    content: Any = None
    query: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    hash: str = ""

    def fingerprint(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "source": self.source,
            "asset_id": self.asset_id,
            "query": self.query,
            "content": self.content,
        }

    def ensure_hash(self) -> str:
        if not self.hash:
            self.hash = evidence_hash(self.fingerprint())
        return self.hash

    def to_dict(self) -> dict[str, Any]:
        self.ensure_hash()
        return asdict(self)


@dataclass
class Anomaly:
    kind: str
    summary: str
    metric: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    strength: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hypothesis:
    id: str
    summary: str
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_count"] = len(self.supporting_evidence)
        return data


@dataclass
class RCAContext:
    incident: dict[str, Any]
    asset: dict[str, Any] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    historical_incidents: list[dict[str, Any]] = field(default_factory=list)
    maintenance: list[dict[str, Any]] = field(default_factory=list)
    playrules: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident": self.incident,
            "asset": self.asset,
            "alerts": self.alerts,
            "evidence": [item.to_dict() for item in self.evidence],
            "anomalies": [item.to_dict() for item in self.anomalies],
            "historical_incidents": self.historical_incidents,
            "maintenance": self.maintenance,
            "playrules": self.playrules,
            "limitations": self.limitations,
        }

    @classmethod
    def from_legacy(cls, context: dict[str, Any]) -> "RCAContext":
        """Accept V0.1 investigation_context dicts."""
        incident = context.get("incident") or {}
        asset = context.get("asset") or {}
        alert = context.get("alert") or {}
        metrics = context.get("metrics") or {}
        logs = context.get("logs") or []
        history = context.get("history") or []
        playrules = context.get("playrules") or []
        maintenance = context.get("maintenance") or []
        asset_id = str(asset.get("asset_id") or incident.get("asset") or "")
        ts = utc_now()
        items: list[EvidenceItem] = []
        seq = 1

        def add(kind: str, source: str, content: Any, query: str = "", extra: dict[str, Any] | None = None) -> None:
            nonlocal seq
            item = EvidenceItem(
                evidence_id=f"EV-{seq:05d}",
                type=kind,
                source=source,
                timestamp=ts,
                asset_id=asset_id,
                content=content,
                query=query,
                metadata=extra or {},
            )
            item.ensure_hash()
            items.append(item)
            seq += 1

        if alert:
            add("ALERT", "alertmanager", alert, extra={"alertname": alert.get("alertname")})
        if asset:
            add("INVENTORY", "forgesre", asset)
        for key, value in metrics.items():
            if key == "error":
                add("METRIC", "prometheus", {"error": value}, extra={"unavailable": True})
                continue
            unit = "percent" if "percent" in key or key in {"cpu", "disk"} else ""
            add(
                "METRIC",
                "prometheus",
                {"type": "metric", "name": key, "value": value, "unit": unit, "timestamp": ts},
                query=str((context.get("queries") or {}).get(key) or key),
            )
        for line in logs[:20]:
            text = line if isinstance(line, str) else str(line)
            add("LOG", "loki", normalize_log(text, ts), query='{job="forgesre"}')
        if history:
            add("INCIDENT_HISTORY", "forgesre", history)
        for rule in playrules:
            add("PLAYRULE", "forgesre", rule)
        for window in maintenance:
            add("MAINTENANCE", "forgesre", window)
        hist_rows = history if isinstance(history, list) else []
        if hist_rows and isinstance(hist_rows[0], dict) and "incidents" in hist_rows[0]:
            hist_rows = hist_rows[0].get("incidents") or []
        return cls(
            incident=incident,
            asset=asset,
            alerts=[alert] if alert else [],
            evidence=items,
            historical_incidents=hist_rows if isinstance(hist_rows, list) else [],
            maintenance=maintenance,
            playrules=playrules,
            limitations=list(context.get("limitations") or []),
        )


def normalize_metric(name: str, value: Any, timestamp: str, unit: str = "") -> dict[str, Any]:
    numeric: float | None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    return {
        "type": "metric",
        "name": name,
        "value": numeric if numeric is not None else value,
        "unit": unit,
        "timestamp": timestamp,
    }


def normalize_log(message: str, timestamp: str) -> dict[str, Any]:
    upper = message.upper()
    if "ERROR" in upper or "FATAL" in upper:
        severity = "ERROR"
    elif "WARN" in upper:
        severity = "WARNING"
    else:
        severity = "INFO"
    return {
        "type": "log",
        "severity": severity,
        "message": message[:400],
        "timestamp": timestamp,
    }
