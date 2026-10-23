"""Four investigation workers plus a compact brief for the ForgeAI writer.

Python only. The LLM never collects; it only rewrites sentences from this brief.
"""

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
    "up": "up",
    "net_rx": "SNMP rx",
    "net_tx": "SNMP tx",
}

ROLLUP_PREFIXES = ("ROLLUP-", "EV-LEGACY")
CUBE_ORDER = ("inventory", "metrics", "logs", "history")


def order_cubes(cubes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Inventory, Metrics, Logs, History — same order on Dashboard and the case page."""
    by_id: dict[str, dict[str, Any]] = {}
    for item in cubes or []:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = item
    return [by_id[key] for key in CUBE_ORDER if key in by_id]


def specialists_from_evidence(rows: Any = None) -> list[dict[str, Any]]:
    """UI cubes: Inventory, Metrics, Logs, History."""
    return specialist_brief(rows)["cubes"]


def specialist_brief(rows: Any = None) -> dict[str, Any]:
    """Structured facts for cubes and the ForgeAI writer."""
    items = [_coerce(row) for row in (rows or []) if _usable(row)]
    collected = bool(items)
    workers = [
        _inventory(items, collected),
        _metrics(items, collected),
        _logs(items, collected),
        _history(items, collected),
    ]
    allowed = " ".join(
        " ".join(str(line) for line in worker.get("facts") or [])
        + " "
        + str(worker.get("detail") or "")
        for worker in workers
    )
    return {"cubes": [_cube_view(worker) for worker in workers], "workers": workers, "allowed": allowed}


def writer_payload(brief: dict[str, Any], incident: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact JSON the writer model sees — specialist facts only."""
    header = {}
    for key in ("number", "title", "severity", "host"):
        value = (incident or {}).get(key)
        if value not in (None, ""):
            header[key] = value
    specialists = {}
    for worker in brief.get("workers") or []:
        facts = [str(line) for line in (worker.get("facts") or []) if str(line).strip()]
        if not facts:
            continue
        specialists[worker["id"]] = {"status": worker["css"], "facts": facts[:8]}
    payload: dict[str, Any] = {"specialists": specialists}
    if header:
        payload["incident"] = header
    return payload


def _cube_view(worker: dict[str, Any]) -> dict[str, Any]:
    facts = [str(line).strip() for line in (worker.get("facts") or []) if str(line).strip()]
    return {
        "id": str(worker.get("id") or ""),
        "label": str(worker.get("label") or ""),
        "css": str(worker.get("css") or "ignored"),
        "detail": str(worker.get("detail") or ""),
        "hint": str(worker.get("hint") or ""),
        "facts": facts[:8],
    }


def _usable(row: Any) -> bool:
    eid = str(getattr(row, "evidence_id", "") or "")
    if isinstance(row, dict):
        eid = str(row.get("evidence_id") or "")
    return not eid.startswith(ROLLUP_PREFIXES)


def _coerce(row: Any) -> dict[str, Any]:
    if isinstance(row, dict) and not hasattr(row, "kind"):
        kind = str(row.get("kind") or row.get("type") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        content = payload.get("content", payload) if isinstance(payload, dict) else {}
        metadata = payload.get("metadata") if isinstance(payload, dict) else {}
        return {"kind": kind.upper(), "content": content or {}, "metadata": metadata or {}}
    kind = str(getattr(row, "kind", "") or getattr(row, "type", "") or "").upper()
    payload = getattr(row, "payload", None)
    if getattr(row, "content", None) is not None and payload is None:
        return {
            "kind": kind,
            "content": getattr(row, "content") or {},
            "metadata": getattr(row, "metadata", None) or {},
        }
    payload = payload or {}
    if isinstance(payload, dict) and "content" in payload:
        content = payload.get("content") or {}
        metadata = payload.get("metadata") or {}
    elif isinstance(row, dict) and row.get("content") is not None:
        content = row.get("content") or {}
        metadata = row.get("metadata") or {}
        kind = str(row.get("type") or row.get("kind") or kind).upper()
    else:
        content = payload if isinstance(payload, dict) else {}
        metadata = {}
    return {"kind": kind, "content": content, "metadata": metadata}


def _pending(label: str, key: str) -> dict[str, Any]:
    return {
        "id": key,
        "label": label,
        "css": "ignored",
        "detail": "Not collected yet.",
        "hint": "Open ForgeRCA to collect this.",
        "facts": [],
    }


def _of(items: list[dict[str, Any]], *kinds: str) -> list[dict[str, Any]]:
    wanted = {k.upper() for k in kinds}
    return [item for item in items if item["kind"] in wanted]


def _broken(item: dict[str, Any]) -> bool:
    content = item["content"] if isinstance(item["content"], dict) else {}
    metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
    return bool(content.get("error") or metadata.get("unavailable"))


def _fmt_metric(name: str, value: float, unit: str) -> str:
    label = LABELS.get(name, name.replace("_", " "))
    if name == "up":
        return "up" if value else "down"
    if unit == "percent" or name.endswith("_percent") or name.endswith("_usage"):
        return f"{label} {value:.0f}%"
    if name in {"net_rx", "net_tx"} or unit == "bytes":
        if value >= 1_000_000:
            return f"{label} {value / 1_000_000:.1f}M"
        if value >= 1000:
            return f"{label} {value / 1000:.1f}k"
        return f"{label} {value:.0f}"
    return f"{label} {value:.0f}"


def _metrics(items: list[dict[str, Any]], collected: bool) -> dict[str, Any]:
    rows = _of(items, "METRIC")
    if not rows:
        out = _pending("Metrics", "metrics")
        if collected:
            out.update(css="warn", detail="No metric samples.", hint="Prometheus returned no values.")
        return out
    if any(_broken(row) for row in rows):
        return {
            "id": "metrics",
            "label": "Metrics",
            "css": "crit",
            "detail": "Prometheus did not answer.",
            "hint": "Metrics collector failed.",
            "facts": ["Prometheus did not answer."],
        }
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
        unit = str(content.get("unit") or "")
        if name == "up" and value == 0:
            down = True
        limit = THRESHOLDS.get(name)
        if limit is not None and value > limit:
            hot = True
        if name == "memory_bytes":
            continue
        line = _fmt_metric(name, value, unit)
        if line and line not in parts:
            parts.append(line)
        if len(parts) >= 5:
            break
    if not parts:
        return {
            "id": "metrics",
            "label": "Metrics",
            "css": "warn",
            "detail": "No metric samples.",
            "hint": "Prometheus returned no values.",
            "facts": [],
        }
    detail = " · ".join(parts[:4])
    css = "ok"
    hint = "Prometheus samples collected."
    if down:
        css, hint = "crit", "Host is not reachable from Prometheus."
    elif hot:
        css, hint = "warn", "A metric is above the ForgeRCA threshold."
    return {"id": "metrics", "label": "Metrics", "css": css, "detail": detail, "hint": hint, "facts": parts}


def _log_text(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("message") or content.get("line") or content.get("text") or "").strip()
    return str(content or "").strip()


def _logs(items: list[dict[str, Any]], collected: bool) -> dict[str, Any]:
    rows = _of(items, "LOG")
    if not rows:
        out = _pending("Logs", "logs")
        if collected:
            out.update(
                css="warn",
                detail="No syslog lines for this host.",
                hint="Loki had nothing for this hostname or IP.",
                facts=["No syslog lines for this host."],
            )
        return out
    if any(_broken(row) for row in rows):
        return {
            "id": "logs",
            "label": "Logs",
            "css": "crit",
            "detail": "Loki did not answer.",
            "hint": "Logs collector failed.",
            "facts": ["Loki did not answer."],
        }
    demo = any((row["metadata"] or {}).get("scope") == "appliance-demo" for row in rows)
    snippets: list[str] = []
    for row in rows:
        text = _log_text(row["content"])
        if text and text not in snippets:
            snippets.append(text[:160])
        if len(snippets) >= 3:
            break
    n = len(rows)
    word = "appliance log line" if demo else "syslog line"
    count = f"{n} {word}{'s' if n != 1 else ''}."
    snippet = snippets[0] if snippets else ""
    detail = f"{count} {snippet}" if snippet else count
    facts = [count, *snippets]
    return {
        "id": "logs",
        "label": "Logs",
        "css": "ok",
        "detail": detail[:140],
        "hint": "Log lines attached as evidence.",
        "facts": facts,
    }


def _inventory(items: list[dict[str, Any]], collected: bool) -> dict[str, Any]:
    rows = _of(items, "INVENTORY")
    maint = _of(items, "MAINTENANCE")
    if not rows:
        out = _pending("Inventory", "inventory")
        if collected:
            out.update(
                css="warn",
                detail="No asset on this incident.",
                hint="Incident has no inventory evidence.",
                facts=["No asset on this incident."],
            )
        return out
    content = rows[0]["content"] if isinstance(rows[0]["content"], dict) else {}
    host = str(content.get("hostname") or content.get("asset_id") or "asset")
    kind = str(content.get("type") or content.get("monitoring_profile") or "").strip()
    source = str(content.get("source") or "").strip().lower()
    source_label = {"netbox": "NetBox", "glpi": "GLPI", "zabbix": "Zabbix", "demo": "DEMO"}.get(source, source)
    facts = [host]
    if kind:
        facts.append(kind)
    if source_label:
        facts.append(f"source {source_label}")
    detail = " · ".join(facts)
    if maint:
        facts.append("maintenance window overlaps")
        return {
            "id": "inventory",
            "label": "Inventory",
            "css": "warn",
            "detail": f"{detail} · maintenance",
            "hint": "A maintenance window overlaps this incident.",
            "facts": facts,
        }
    return {
        "id": "inventory",
        "label": "Inventory",
        "css": "ok",
        "detail": detail,
        "hint": "Asset inventory collected.",
        "facts": facts,
    }


def _history(items: list[dict[str, Any]], collected: bool) -> dict[str, Any]:
    rows = _of(items, "INCIDENT_HISTORY")
    play = _of(items, "PLAYRULE")
    if not rows and not play:
        out = _pending("History", "history")
        if collected:
            out.update(
                css="ok",
                detail="No earlier incidents on this asset.",
                hint="Nothing to recall yet.",
                facts=["No earlier incidents on this asset."],
            )
        return out
    earlier: list[dict[str, Any]] = []
    if rows:
        content = rows[0]["content"]
        if isinstance(content, list):
            earlier = [row for row in content if isinstance(row, dict)]
        elif isinstance(content, dict):
            raw = content.get("incidents") or content.get("history") or []
            earlier = [row for row in raw if isinstance(row, dict)]
    n = len(earlier)
    facts: list[str] = []
    titles = []
    for row in earlier[:3]:
        title = str(row.get("title") or row.get("number") or "").strip()
        number = str(row.get("number") or "").strip()
        if title and number and title != number:
            titles.append(f"{number} {title}")
        elif title:
            titles.append(title)
    if n:
        facts.append(f"{n} earlier on this asset.")
        facts.extend(titles)
        detail = f"{n} earlier on this asset."
        if titles:
            detail = f"{detail} {titles[0]}"
        css = "warn" if n >= 3 else "ok"
    else:
        facts.append("No earlier incidents on this asset.")
        detail = facts[0]
        css = "ok"
    book = ""
    if play:
        body = play[0]["content"] if isinstance(play[0]["content"], dict) else {}
        book = str(body.get("playbook") or body.get("name") or "").strip()
    if book:
        facts.append(f"playbook {book}")
        detail = f"{detail} · {book}"
    return {
        "id": "history",
        "label": "History",
        "css": css,
        "detail": detail[:140],
        "hint": "Previous incidents and the matched playbook.",
        "facts": facts,
    }
