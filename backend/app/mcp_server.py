"""Read-only MCP server at /mcp, so an AI client (Claude, Cursor, ...) can ask ForgeSRE questions.

Off by default (``mcp.enabled``). When on, a client sends ``Authorization: Bearer <MCP_TOKEN>``.
Every tool only reads; there is no tool that acknowledges, edits or deletes anything.

Data leaves the appliance when this is used: the client passes tool results to whatever
model it runs. Tool results therefore carry no evidence queries, no notification bodies,
no email addresses or phone numbers of people, and no secrets.

Built on the official MCP Python SDK (stateless Streamable HTTP, JSON responses), which
negotiates the protocol version with the client.
"""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.db import SessionLocal
from app.models import Asset, Incident, User
from app.settings import settings

INSTRUCTIONS = (
    "ForgeSRE is the NOC/SRE console of an on-premise data center. Use noc_overview first to see what is "
    "open, get_incident for one incident (ForgeRCA facts, timeline, what changed before), search_assets "
    "and asset_dependencies for inventory, on_call_now, slo_status and checks_status. All tools are read-only."
)


def enabled() -> bool:
    return bool(((settings.yaml or {}).get("mcp") or {}).get("enabled", False))


def token() -> str:
    return settings._secret("MCP_TOKEN")


def _viewer() -> User:
    """Identity for views: Viewer rights, so evidence queries stay out of tool results."""
    return User(id=0, email="mcp", name="MCP", role="viewer", is_active=True)


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    keep = ("number", "severity", "status", "title", "detail", "source", "started_at", "ack_at", "resolved_at", "level", "forgerca")
    out = {k: row.get(k) for k in keep if k in row}
    asset = row.get("asset") or {}
    out["asset"] = {"hostname": asset.get("hostname"), "type": asset.get("type")} if asset else None
    return out


def noc_overview() -> dict[str, Any]:
    """Open incidents right now: counts, the active and acknowledged incidents, health by asset group."""
    from app.noc import noc_overview as overview

    with SessionLocal() as db:
        data = overview(db)
    return {
        "counts": {k: v for k, v in data["counts"].items() if k != "hosts_down_names"},
        "hosts_down": data["counts"].get("hosts_down_names", []),
        "active": [_brief(i) for i in data["incidents"]["active"]],
        "acknowledged": [_brief(i) for i in data["incidents"]["acknowledged"]],
        "health": data["health"],
    }


def list_incidents(status: str = "active", severity: str = "", query: str = "", limit: int = 20) -> dict[str, Any]:
    """Incident list. status: active | acknowledged | resolved | all. severity: critical | high | warning."""
    from app.incident_view import incidents_page

    with SessionLocal() as db:
        page = incidents_page(db, tab=status, severity=severity, q=query, limit=max(1, min(limit, 100)))
    return {"counts": page["counts"], "total": page["total"], "incidents": [_brief(r) for r in page["rows"]]}


def get_incident(number: str) -> dict[str, Any]:
    """One incident (e.g. INC-0412): ForgeRCA facts and hypotheses, ForgeAI summary, timeline, notes,
    what changed in the 24 h before, playbook progress and the review's root cause."""
    from app.incident_view import incident_view

    with SessionLocal() as db:
        row = db.query(Incident).filter_by(number=number.strip()).first()
        if row is None:
            return {"error": f"no incident {number}"}
        view = incident_view(db, row, _viewer())
    asset = view.get("asset") or {}
    return {
        **{k: view[k] for k in ("number", "title", "severity", "status", "source", "summary", "started_at", "ack_at",
                                "resolved_at", "level", "playrule", "playbook", "forgerca", "review", "changes", "timeline")},
        "forgeai": {k: view["forgeai"][k] for k in ("text", "cause", "action", "model")},
        "asset": {k: asset.get(k) for k in ("hostname", "type", "ip", "environment", "owner")} if asset else None,
        "notes": [{"at": n["at"], "body": n["body"]} for n in view.get("notes") or []],
        "notifications": [{"channel": n.get("channel"), "status": n.get("status"), "step": n.get("step_key"), "at": n.get("created_at")}
                          for n in view.get("notifications") or []],
    }


def search_assets(query: str = "", view: str = "", limit: int = 25) -> dict[str, Any]:
    """Inventory search by hostname, IP, type or owner. view: down | alarm | no_source | no_owner (optional)."""
    from app.inventory_view import inventory_page

    with SessionLocal() as db:
        page = inventory_page(db, q=query, view=view, limit=max(1, min(limit, 100)))
    keep = ("asset_id", "hostname", "ip", "type", "environment", "owner", "site", "rack", "state", "sources")
    return {"total": page["total"], "assets": [{k: r.get(k) for k in keep if k in r} for r in page["rows"]]}


def asset_dependencies(asset: str) -> dict[str, Any]:
    """Where an asset sits in the dependency map: its parents up to the root and what is behind it."""
    from sqlalchemy import or_

    with SessionLocal() as db:
        row = db.query(Asset).filter(or_(Asset.hostname == asset, Asset.asset_id == asset, Asset.ip == asset)).first()
        if row is None:
            return {"error": f"no asset {asset}"}
        chain, cursor = [], row.parent
        while cursor is not None and len(chain) < 12:
            chain.append({"hostname": cursor.hostname, "type": cursor.type})
            cursor = cursor.parent
        behind, frontier = [], [row.id]
        for _ in range(12):
            kids = db.query(Asset).filter(Asset.parent_id.in_(frontier)).all()
            if not kids:
                break
            behind += [k.hostname for k in kids]
            frontier = [k.id for k in kids]
        return {"hostname": row.hostname, "type": row.type, "parents": chain, "behind": behind[:200], "behind_count": len(behind)}


def on_call_now() -> dict[str, Any]:
    """Who is on call this minute (name and time window only)."""
    from app.dc_noc import current_oncall

    with SessionLocal() as db:
        slot = current_oncall(db)
    return {"on_call": {"name": slot.get("person") or slot.get("name"), "window": slot.get("window")} if slot else None}


def slo_status() -> dict[str, Any]:
    """Every SLO: availability against target, error budget used and left, 24 h burn rate."""
    from app.slo import evaluate_all

    with SessionLocal() as db:
        return {"slos": [{k: v for k, v in r.items() if k != "owner"} for r in evaluate_all(db)]}


def checks_status() -> dict[str, Any]:
    """Certificate expiry, ForgeSRE backup age and heartbeats, latest results."""
    from app.health_checks import results

    with SessionLocal() as db:
        data = results(db)
    return {"results": data["results"], "heartbeats": [{k: h[k] for k in ("name", "every_hours", "last_at")} for h in data["heartbeats"]]}


TOOLS = (noc_overview, list_incidents, get_incident, search_assets, asset_dependencies, on_call_now, slo_status, checks_status)


def build_server() -> MCPServer:
    server = MCPServer(name="forgesre", title="ForgeSRE (read-only)", instructions=INSTRUCTIONS, version="0.9")
    from mcp.types import ToolAnnotations

    for fn in TOOLS:
        server.add_tool(fn, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    return server


class Guard:
    """ASGI wrapper: 404 while disabled, 401 without the bearer token, 403 for a browser Origin
    that is not ForgeSRE's own public address (DNS rebinding)."""

    def __init__(self, inner: ASGIApp):
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        expected = token()
        if not enabled() or not expected:
            return await JSONResponse({"error": "MCP is off (mcp.enabled and MCP_TOKEN)"}, status_code=404)(scope, receive, send)
        given = headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return await JSONResponse({"error": "bearer MCP_TOKEN required"}, status_code=401)(scope, receive, send)
        origin = headers.get("origin", "")
        public = str(((settings.yaml or {}).get("system") or {}).get("public_url") or "")
        if origin and urlparse(origin).netloc != urlparse(public).netloc:
            return await JSONResponse({"error": "origin not allowed"}, status_code=403)(scope, receive, send)
        return await self.inner(scope, receive, send)


def asgi_app(server: MCPServer) -> ASGIApp:
    # Host checks are done by Guard (token + Origin); the appliance is reached by IP or any name.
    inner = server.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return Guard(inner)
