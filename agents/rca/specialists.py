"""Named investigation specialists. Python facts only — not extra LLMs."""

from __future__ import annotations

from typing import Any

THRESHOLDS = {
    "cpu_percent": 80.0,
    "cpu_usage": 80.0,
    "disk_percent": 80.0,
    "filesystem_usage": 80.0,
    "disk_volume_percent": 80.0,
    "memory_percent": 90.0,
    "memory_usage": 90.0,
}

LABELS = {
    "cpu_percent": "CPU",
    "cpu_usage": "CPU",
    "disk_percent": "disk",
    "filesystem_usage": "disk",
    "disk_volume_percent": "disk",
    "memory_percent": "mem",
    "memory_usage": "mem",
}

ROLLUP_PREFIXES = ("ROLLUP-", "EV-LEGACY")


def specialists_from_evidence(rows: Any = None) -> list[dict[str, str]]:
    """Four cubes: Metrics, Logs, Inventory, History. Derived from stored evidence."""
    items = [_coerce(row) for row in (rows or []) if _usable(row)]
    collected = bool(items)
    return [
        _metrics(items, collected),
        _logs(items, collected),
        _inventory(items, collected),
        _history(items, collected),
    ]


def _usable(row: Any) -> bool:
    eid = str(getattr(row, "evidence_id", "") or "")
    if isinstance(row, dict):
        eid = str(row.get("evidence_id") or "")
    return not eid.startswith(ROLLUP_PREFIXES)


def _coerce(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        kind = str(row.get("kind") or row.get("type") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        content = payload.get("content", payload) if isinstance(payload, dict) else {}
        metadata = payload.get("metadata") if isinstance(payload, dict) else {}
        return {"kind": kind.upper(), "content": content or {}, "metadata": metadata or {}}
    kind = str(getattr(row, "kind", "") or getattr(row, "type", "") or "").upper()
    payload = getattr(row, "payload", None) or {}
    if isinstance(payload, dict) and "content" in payload:
        content = payload.get("content") or {}
        metadata = payload.get("metadata") or {}
    else:
        content = payload if isinstance(payload, dict) else {}
        metadata = {}
    return {"kind": kind, "content": content, "metadata": metadata}


def _cube(key: str, label: str, css: str, detail: str, hint: str) -> dict[str, str]:
    return {"id": key, "label": label, "css": css, "detail": detail, "hint": hint}


def _pending(label: str, key: str) -> dict[str, str]:
    return _cube(key, label, "ignored", "Not collected yet.", "Open ForgeRCA to collect this.")


def _of(items: list[dict[str, Any]], *kinds: str) -> list[dict[str, Any]]:
    wanted = {k.upper() for k in kinds}
    return [item for item in items if item["kind"] in wanted]


def _broken(item: dict[str, Any]) -> bool:
    content = item["content"] if isinstance(item["content"], dict) else {}
    metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
    return bool(content.get("error") or metadata.get("unavailable"))


def _metrics(items: list[dict[str, Any]], collected: bool) -> dict[str, str]:
    rows = _of(items, "METRIC")
    if not rows:
        return _pending("Metrics", "metrics") if not collected else _cube(
            "metrics", "Metrics", "warn", "No metric samples.", "Prometheus returned no values."
        )
    if any(_broken(row) for row in rows):
        return _cube("metrics", "Metrics", "crit", "Prometheus did not answer.", "Metrics collector failed.")
    parts: list[str] = []
    hot = False
    down = False
    for row in rows:
        content = row["content"] if isinstance(row["content"], dict) else {}
        name = str(content.get("name") or "")
        try:
            value = float(content.get("value"))
        except (TypeError, ValueError):
            continue
        if name == "up":
            if value == 0:
                down = True
                parts.append("down")
            else:
                parts.append("up")
            continue
        unit = str(content.get("unit") or "")
        if unit == "percent" or name.endswith("_percent") or name.endswith("_usage"):
            label = LABELS.get(name, name.replace("_", " "))
            parts.append(f"{label} {value:.0f}%")
            limit = THRESHOLDS.get(name)
            if limit is not None and value > limit:
                hot = True
        if len(parts) >= 3:
            break
    if not parts:
        return _cube("metrics", "Metrics", "warn", "No metric samples.", "Prometheus returned no values.")
    detail = " · ".join(parts)
    if down:
        return _cube("metrics", "Metrics", "crit", detail, "Host is not reachable from Prometheus.")
    if hot:
        return _cube("metrics", "Metrics", "warn", detail, "A metric is above the ForgeRCA threshold.")
    return _cube("metrics", "Metrics", "ok", detail, "Prometheus samples collected.")


def _logs(items: list[dict[str, Any]], collected: bool) -> dict[str, str]:
    rows = _of(items, "LOG")
    if not rows:
        return _pending("Logs", "logs") if not collected else _cube(
            "logs", "Logs", "warn", "No syslog lines for this host.", "Loki had nothing for this hostname or IP."
        )
    if any(_broken(row) for row in rows):
        return _cube("logs", "Logs", "crit", "Loki did not answer.", "Logs collector failed.")
    demo = any((row["metadata"] or {}).get("scope") == "appliance-demo" for row in rows)
    n = len(rows)
    word = "appliance log line" if demo else "syslog line"
    detail = f"{n} {word}{'s' if n != 1 else ''}."
    return _cube("logs", "Logs", "ok", detail, "Log lines attached as evidence.")


def _inventory(items: list[dict[str, Any]], collected: bool) -> dict[str, str]:
    rows = _of(items, "INVENTORY")
    maint = _of(items, "MAINTENANCE")
    if not rows:
        return _pending("Inventory", "inventory") if not collected else _cube(
            "inventory", "Inventory", "warn", "No asset on this incident.", "Incident has no inventory evidence."
        )
    content = rows[0]["content"] if isinstance(rows[0]["content"], dict) else {}
    host = str(content.get("hostname") or content.get("asset_id") or "asset")
    kind = str(content.get("type") or content.get("monitoring_profile") or "").strip()
    detail = f"{host} · {kind}" if kind else host
    if maint:
        return _cube(
            "inventory",
            "Inventory",
            "warn",
            f"{detail} · maintenance",
            "A maintenance window overlaps this incident.",
        )
    return _cube("inventory", "Inventory", "ok", detail, "Asset inventory collected.")


def _history(items: list[dict[str, Any]], collected: bool) -> dict[str, str]:
    rows = _of(items, "INCIDENT_HISTORY")
    play = _of(items, "PLAYRULE")
    if not rows and not play:
        return _pending("History", "history") if not collected else _cube(
            "history", "History", "ok", "No earlier incidents on this asset.", "Nothing to recall yet."
        )
    n = 0
    if rows:
        content = rows[0]["content"]
        if isinstance(content, list):
            n = len(content)
        elif isinstance(content, dict):
            n = len(content.get("incidents") or content.get("history") or [])
    book = ""
    if play:
        body = play[0]["content"] if isinstance(play[0]["content"], dict) else {}
        book = str(body.get("playbook") or body.get("name") or "").strip()
    if n:
        detail = f"{n} earlier on this asset."
        css = "warn" if n >= 3 else "ok"
    else:
        detail = "No earlier incidents on this asset."
        css = "ok"
    if book:
        detail = f"{detail} · {book}"
    return _cube("history", "History", css, detail, "Previous incidents and the matched playbook.")
