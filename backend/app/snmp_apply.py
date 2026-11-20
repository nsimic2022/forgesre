"""Apply per-asset SNMP auths from Core, so a device added in the UI is walked without a shell step.

Same result as ``./forgesre snmp-auths`` (scripts/render_snmp_auths.py): rewrite the block between
the ``forgesre-asset-auths`` markers in the generated snmp.yml (Core mounts it at
/host-generated) and ask snmp_exporter to reload. SNMP HTTP SD switches the asset to its own auth
as soon as the file has it. Runs in the ``snmp_apply`` job; secrets are never logged or returned.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

BEGIN = "  # forgesre-asset-auths begin"
END = "  # forgesre-asset-auths end"
RELOAD_URL = "http://127.0.0.1:9116/-/reload"
TIMEOUT = 5.0
JOB_KIND = "snmp_apply"


def splice(text: str, block: str) -> str:
    """Replace what sits between the markers with ``block``; the rest of the file is untouched."""
    if BEGIN not in text or END not in text:
        return text
    head, rest = text.split(BEGIN, 1)
    begin_line, _, _old = rest.partition("\n")
    tail = rest.split(END, 1)[1]
    body = block if not block or block.endswith("\n") else block + "\n"
    return f"{head}{BEGIN}{begin_line}\n{body}{END}{tail}"


def reload_exporter(url: str = RELOAD_URL) -> bool:
    try:
        return httpx.post(url, timeout=TIMEOUT).status_code < 300
    except httpx.HTTPError:
        return False


def apply_auths(db: Session, path: Path | None = None, *, reload: bool = True) -> dict[str, Any]:
    """Write the auth block for every SNMP asset with a custom community or v3 user.

    Returns {"status": ok|unchanged|error, "auths": n, "reloaded": bool, "why": str}; the journal
    gets the same line. A missing file or markers is an error with the command that fixes it.
    """
    from app.asset_snmp import dump_auth_block, generated_snmp_path
    from app.inventory import snmp_auths
    from app.journal import report

    target = path or generated_snmp_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        result = {"status": "error", "auths": 0, "reloaded": False,
                  "why": f"{target} is missing; run ./forgesre render-monitoring on the appliance"}
        report(db, "snmp", "auths.apply", "error", summary=result["why"])
        return result
    if BEGIN not in text or END not in text:
        result = {"status": "error", "auths": 0, "reloaded": False,
                  "why": "snmp.yml has no forgesre-asset-auths markers; run ./forgesre render-monitoring"}
        report(db, "snmp", "auths.apply", "error", summary=result["why"])
        return result
    auths = snmp_auths(db)
    new = splice(text, dump_auth_block(auths))
    if new == text:
        result = {"status": "unchanged", "auths": len(auths), "reloaded": False, "why": "snmp.yml already has these auths"}
    else:
        target.write_text(new, encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        reloaded = reload_exporter() if reload else False
        why = "snmp_exporter reloaded" if reloaded else "written; snmp_exporter did not reload (docker compose restart snmp-exporter)"
        result = {"status": "ok" if reloaded or not reload else "error", "auths": len(auths), "reloaded": reloaded, "why": why}
    report(db, "snmp", "auths.apply", "ok" if result["status"] != "error" else "error",
           summary=f"Per-asset SNMP auths: {len(auths)} ({result['why']})")
    return result


def queue_if_needed(db: Session, asset: Any, actor: str = "system") -> bool:
    """Queue ``snmp_apply`` when this SNMP asset wants its own auth that snmp.yml does not have yet."""
    from app.asset_snmp import auth_status
    from app.inventory import is_snmp_asset
    from app.jobs import enqueue

    if not is_snmp_asset(asset) or not auth_status(asset).get("pending"):
        return False
    enqueue(db, JOB_KIND, "snmp", object_type="snmp", payload={"actor": actor})
    return True
