"""Read-only view of Prometheus alert rules (monitoring/alerts.yml + alerts.local.yml).

Playrules map an alertname to a playbook. The PromQL that decides *when* an alert
fires lives only in these files. Core never writes them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

RULE_FILES = ("alerts.yml", "alerts.local.yml")


def monitoring_dir() -> Path:
    env = os.environ.get("FORGESRE_MONITORING_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "monitoring"


def load_alert_rules(base: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """alertname → [{expr, for, severity, file}]. Missing or broken files yield an empty map."""
    root = base or monitoring_dir()
    out: dict[str, list[dict[str, Any]]] = {}
    for name in RULE_FILES:
        path = root / name
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        groups = data.get("groups") if isinstance(data, dict) else None
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            for rule in group.get("rules") or []:
                if not isinstance(rule, dict) or not rule.get("alert"):
                    continue
                labels = rule.get("labels") if isinstance(rule.get("labels"), dict) else {}
                out.setdefault(str(rule["alert"]), []).append(
                    {
                        "expr": " ".join(str(rule.get("expr") or "").split()),
                        "for": str(rule.get("for") or ""),
                        "severity": str(labels.get("severity") or ""),
                        "file": name,
                    }
                )
    return out


def rules_for(alertname: str, rules: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    table = load_alert_rules() if rules is None else rules
    wanted = (alertname or "").strip().lower()
    for key, items in table.items():
        if key.lower() == wanted:
            return items
    return []
