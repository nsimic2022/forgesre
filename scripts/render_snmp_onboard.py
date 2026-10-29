#!/usr/bin/env python3
"""Splice forgesre_onboard into data/generated/snmp.yml after render-monitoring rewrites the template.

Reads extra OIDs from Core GET /api/v1/sd/snmp-onboard-config (no secrets). If Core is down,
keeps the previous onboard block or onboard-module.yml sidecar. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BEGIN = "  # forgesre-onboard-modules begin"
END = "  # forgesre-onboard-modules end"


def onboard_block(text: str) -> str | None:
    if BEGIN not in text or END not in text:
        return None
    inner = text.split(BEGIN, 1)[1].split(END, 1)[0]
    lines = inner.split("\n")[1:]
    return "\n".join(lines)


def splice(text: str, block: str) -> str:
    if BEGIN not in text or END not in text:
        return text
    head, rest = text.split(BEGIN, 1)
    begin_line, _, _old = rest.partition("\n")
    tail = rest.split(END, 1)[1]
    body = block if not block or block.endswith("\n") else block + "\n"
    return f"{head}{BEGIN}{begin_line}\n{body}{END}{tail}"


def fetch_config(port: str, token: str, timeout: float = 5.0) -> dict | None:
    if not token:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/sd/snmp-onboard-config",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sidecar_block(snmp_path: Path) -> str:
    try:
        return (snmp_path.parent / "onboard-module.yml").read_text(encoding="utf-8")
    except OSError:
        return ""


def apply(path: Path, port: str, token: str, *, previous_block: str | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text:
        return f"{path} has no forgesre-onboard-modules markers — run ./forgesre render-monitoring first."
    cfg = fetch_config(port, token)
    if cfg is None:
        kept = previous_block if previous_block is not None else (onboard_block(text) or "")
        if not str(kept or "").strip():
            kept = sidecar_block(path)
        if kept != (onboard_block(text) or ""):
            path.write_text(splice(text, kept), encoding="utf-8")
        present = "forgesre_onboard:" in kept
        return (
            "Core did not answer /api/v1/sd/snmp-onboard-config — "
            + ("kept existing forgesre_onboard module." if present else "no extra onboard module.")
        )
    block = str(cfg.get("block") or "")
    new = splice(text, block)
    if new != text:
        path.write_text(new, encoding="utf-8")
    n = len(cfg.get("oids") or [])
    return f"Onboard SNMP module in snmp.yml: {n} OID(s)."


def main(argv: list[str]) -> int:
    data = Path(os.environ.get("FORGESRE_DATA") or "./data")
    path = Path(argv[1]) if len(argv) > 1 else data / "generated" / "snmp.yml"
    port = os.environ.get("FORGESRE_HTTP_PORT") or "8080"
    token = os.environ.get("ALERTMANAGER_WEBHOOK_TOKEN") or ""
    if not path.is_file():
        print(f"Missing {path}. Run ./forgesre render-monitoring first.")
        return 1
    print(apply(path, port, token))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
