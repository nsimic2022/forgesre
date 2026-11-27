"""AI client → ForgeSRE MCP server, block by block (registered in app.flows).

Settings (mcp.enabled, MCP_TOKEN set) → public address → the last calls (who, refused or
accepted). Read-only tools only; tool results leave the server to the client's model, which the
page says plainly. The token is never shown or logged.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.flows import Flow, forgesre_port, register, step
from app.settings import settings


def check(db: Session) -> dict[str, Any]:
    """Settings, then the call log written by the MCP Guard."""
    from app.exchanges import list_exchanges
    from app.mcp_server import enabled, token

    on, has = enabled(), bool(token())
    steps = [step("cfg", "Settings", "mcp.enabled", "ok" if on and has else ("off" if not on else "bad"),
                  ("On, MCP_TOKEN set." if on and has else "Off (default): AI clients cannot reach ForgeSRE." if not on else "On, but MCP_TOKEN is empty."),
                  fix="" if on and has else ("Optional: ./forgesre settings set mcp.enabled true" if not on else "./forgesre settings set-secret MCP_TOKEN"))]
    if on and has:
        public = str(((settings.yaml or {}).get("system") or {}).get("public_url") or "")
        steps.append(step("address", "Address", public or f"http://<FORGE-IP>:{forgesre_port()}", "ok" if public else "warn",
                          f"Clients connect to {public or 'http://<FORGE-IP>:' + forgesre_port()}/mcp with Authorization: Bearer <MCP_TOKEN>.",
                          fix="" if public else "Set system.public_url so browser-based clients pass the Origin check."))
        calls = list_exchanges(db, "mcp", limit=20)["rows"]
        refused = [c for c in calls if c["status"] != "ok"]
        rows = [{"at": c["at"], "status": c["status"], "error": c["summary"] if c["status"] != "ok" else "", "subject": f"{c['peer']} · {c['summary']}"} for c in calls]
        steps.append(step("calls", "Calls", f"{len(calls)} recent", "unknown" if not calls else ("bad" if calls[0]["status"] != "ok" else ("warn" if refused else "ok")),
                          (f"Last: {calls[0]['summary']} ({calls[0]['status']}); {len(refused)} of the last {len(calls)} refused." if calls else "No AI client has called yet."),
                          rows=rows, log={"direction": "in", "kinds": ["mcp"]},
                          fix="A 401 is a client with the wrong token; a 403 a browser page from another address." if refused else ""))
        steps.append(step("data", "Data", "leaves the server", "warn",
                          "Tool results go to the client's own model. They carry no evidence queries, people's contacts or secrets."))
    return {"lanes": [{"id": "in", "title": "AI client → ForgeSRE MCP (read-only tools)", "steps": steps}]}


def _configured() -> bool:
    from app.mcp_server import enabled

    return enabled()


register(Flow(name="mcp", label="MCP server", check=check, configured=_configured, log_words=("mcp",),
              about="How an AI client reaches ForgeSRE's read-only MCP tools: settings, address, and who called."))
