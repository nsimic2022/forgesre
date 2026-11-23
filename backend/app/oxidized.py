"""Network config changes from Oxidized, as "changes before incident".

Oxidized backs up device configs and keeps their history in git. Every 10 minutes the leader
queues job ``oxidized_sync``, which reads ``GET {url}/nodes.json`` (5 s timeout, optional
Bearer ``OXIDIZED_TOKEN`` for a reverse proxy). A node whose ``mtime`` (last config change)
moved since the previous read becomes a ChangeEvent (source ``oxidized``, kind ``config``) on
the matching asset (name or IP), so an incident right after a config push shows it.
The first read only records the baseline. Per-node state lives in ``config_nodes``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.models import ConfigNode, utcnow
from app.settings import settings

JOB_KIND = "oxidized_sync"
TIMEOUT = 5.0


def _cfg() -> dict[str, Any]:
    raw = ((settings.yaml or {}).get("changes") or {}).get("oxidized") or {}
    return raw if isinstance(raw, dict) else {}


def ready() -> str:
    """Why the sync cannot run, or "" when it can."""
    if not _cfg().get("enabled"):
        return "Oxidized is off (changes.oxidized.enabled)"
    if not str(_cfg().get("url") or "").strip():
        return "changes.oxidized.url is empty"
    return ""


def _url(path: str) -> str:
    return str(_cfg().get("url") or "").rstrip("/") + path


def fetch_nodes(client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Oxidized node list. Raises httpx errors on failure (the job records them)."""
    headers = {"Accept": "application/json"}
    token = settings._secret("OXIDIZED_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = client.get(_url("/nodes.json"), headers=headers)
        response.raise_for_status()
        data = response.json()
    finally:
        if own:
            client.close()
    return [n for n in data if isinstance(n, dict) and n.get("name")] if isinstance(data, list) else []


def _mtime(node: dict[str, Any]) -> str:
    """Last config change as Oxidized reports it ('unknown' when the output has no history)."""
    return str(node.get("mtime") or "").strip()


def _parse(value: str) -> datetime | None:
    text = value.replace(" UTC", "+00:00").replace(" ", "T", 1) if value else ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sync(db: Session, nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Compare nodes with the stored state; record one ChangeEvent per changed config."""
    from app.changes import record

    known = {row.full_name: row for row in db.query(ConfigNode).all()}
    changed = failed = 0
    for node in nodes:
        full = str(node.get("full_name") or node.get("name"))[:255]
        mtime = _mtime(node)
        status = str((node.get("last") or {}).get("status") or node.get("status") or "")[:32]
        failed += status not in {"", "success"}
        row = known.get(full)
        if row is None:
            db.add(ConfigNode(full_name=full, name=str(node["name"])[:255], ip=str(node.get("ip") or "")[:64],
                              mtime=mtime[:64], last_status=status, checked_at=utcnow()))
            continue
        if mtime and mtime != "unknown" and mtime != row.mtime and row.mtime:
            ref = str(node.get("ip") or "") or row.name
            match = row.name
            from app.models import Asset

            if db.query(Asset).filter((Asset.hostname == row.name) | (Asset.asset_id == row.name)).first() is None:
                match = ref
            record(db, title=f"Config changed on {row.name}", source="oxidized", kind="config",
                   detail=f"Oxidized saved a new configuration version ({node.get('model') or 'unknown model'}).",
                   url=_url(f"/node/version?node_full={quote(full)}") if _cfg().get("url") else "",
                   actor="oxidized", asset_ref=match,
                   at=_parse(mtime))
            changed += 1
        row.mtime = mtime[:64] or row.mtime
        row.last_status, row.checked_at = status, utcnow()
    db.commit()
    return {"nodes": len(nodes), "changed": changed, "failed_backups": failed}


def run_job(db: Session) -> dict[str, int]:
    """Background job body: fetch and sync; errors propagate so the job shows them."""
    return sync(db, fetch_nodes())


def check() -> dict[str, Any]:
    """Settings test: reach Oxidized and count nodes."""
    why = ready()
    if why:
        return {"status": "disabled" if "is off" in why else "error", "why": why}
    try:
        nodes = fetch_nodes()
    except Exception as exc:  # network errors, bad JSON, 401 from a proxy
        return {"status": "error", "why": f"{_url('/nodes.json')}: {type(exc).__name__}: {exc}"[:300]}
    return {"status": "ok", "why": f"{len(nodes)} nodes"}
