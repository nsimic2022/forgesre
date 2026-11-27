"""ForgeSRE → Oxidized (device config backups), block by block (registered in app.flows).

Settings → DNS/TCP(/TLS) → /nodes.json → devices whose last backup failed → nodes known to
ForgeSRE (a new config version shows as a change). Read-only, 5 s; the token is never shown.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.flows import Flow, network_steps, register, step


def check(db: Session) -> dict[str, Any]:
    """Settings, network, the node list and failed backups, then what ForgeSRE keeps."""
    from app import oxidized
    from app.models import ConfigNode

    why = oxidized.ready()
    if why:
        return {"lanes": [{"id": "out", "title": "ForgeSRE → Oxidized", "steps": [
            step("cfg", "Settings", "changes.oxidized", "off" if "is off" in why else "bad", f"{why}.",
                 fix="Optional: set changes.oxidized.url and changes.oxidized.enabled: true")]}]}
    url = oxidized._url("")
    steps = [step("cfg", "Settings", url, "ok", f"Reads {url}/nodes.json.")]
    net, ok = network_steps(url, what="Oxidized")
    steps += net
    if ok:
        started = time.monotonic()
        try:
            nodes = oxidized.fetch_nodes()
        except (httpx.HTTPError, ValueError) as exc:
            steps.append(step("nodes", "Nodes", "/nodes.json", "bad", f"No node list: {exc.__class__.__name__}: {exc}"[:240] + ".",
                              ms=int((time.monotonic() - started) * 1000), fix="A 401 is OXIDIZED_TOKEN (or the proxy in front of Oxidized)."))
        else:
            failed = [n for n in nodes if str(n.get("status") or "").lower() not in {"success", ""}]
            rows = [{"at": str(((n.get("last") or {}).get("end")) or ""), "status": str(n.get("status") or ""), "error": "",
                     "subject": f"{n.get('name')} ({n.get('ip') or ''})"} for n in failed[:20]]
            steps.append(step("nodes", "Nodes", f"{len(nodes)} device(s)", "ok" if nodes and not failed else ("warn" if nodes else "unknown"),
                              (f"{len(nodes)} device(s); {len(failed)} with a failed last backup." if nodes else "Oxidized has no device."),
                              ms=int((time.monotonic() - started) * 1000), rows=rows,
                              fix="Check those devices' credentials and model in Oxidized." if failed else ""))
    known = db.query(ConfigNode).count()
    steps.append(step("known", "ForgeSRE", f"{known} node(s)", "ok" if known else "unknown",
                      f"ForgeSRE follows {known} device config(s); a new version is recorded as a change." if known
                      else "ForgeSRE has not read Oxidized yet (the sync runs every 15 minutes)."))
    return {"lanes": [{"id": "out", "title": "ForgeSRE → Oxidized (read-only)", "steps": steps}]}


def _configured() -> bool:
    from app.oxidized import ready

    return not ready()


register(Flow(name="oxidized", label="Oxidized", check=check, configured=_configured, log_words=("oxidized",),
              about="How ForgeSRE reads device config backups from Oxidized to show config changes before an incident."))
