#!/usr/bin/env python3
"""Write per-asset snmp_exporter auths (Custom community / SNMPv3) into data/generated/snmp.yml.

Core keeps the settings (Assets → Edit → Comms / monitoring). This host-side step reads them from
GET /api/v1/sd/snmp-auths (Bearer ALERTMANAGER_WEBHOOK_TOKEN), rewrites the block between the
``forgesre-asset-auths`` markers, and asks snmp_exporter to reload. Secrets are never printed.

If Core does not answer, the block already in snmp.yml is kept as it was. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BEGIN = "  # forgesre-asset-auths begin"
END = "  # forgesre-asset-auths end"
_ORDER = ("version", "community", "username", "security_level", "auth_protocol", "password", "priv_protocol", "priv_password")


def asset_block(text: str) -> str | None:
    """Lines between the markers (without them), or None when the file has no markers."""
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


def yaml_block(auths: dict) -> str:
    lines: list[str] = []
    for name in sorted(auths):
        spec = auths[name]
        if not isinstance(spec, dict):
            continue
        lines.append(f"  {json.dumps(str(name))}:")
        for key in _ORDER:
            if key not in spec:
                continue
            value = spec[key]
            rendered = str(int(value)) if key == "version" else json.dumps(str(value))
            lines.append(f"    {key}: {rendered}")
    return "\n".join(lines) + ("\n" if lines else "")


def fetch_auths(port: str, token: str, timeout: float = 5.0) -> dict | None:
    if not token:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/sd/snmp-auths",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    auths = data.get("auths") if isinstance(data, dict) else None
    return auths if isinstance(auths, dict) else None


def reload_exporter(url: str = "http://127.0.0.1:9116/-/reload") -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def apply(path: Path, port: str, token: str, *, previous_block: str | None = None, reload: bool = True) -> str:
    """Rewrite the asset-auth block in ``path``. Returns a one-line status (no secrets)."""
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text:
        return f"{path} has no forgesre-asset-auths markers — run ./forgesre render-monitoring first."
    auths = fetch_auths(port, token)
    if auths is None:
        kept = previous_block if previous_block is not None else (asset_block(text) or "")
        if kept != (asset_block(text) or ""):
            path.write_text(splice(text, kept), encoding="utf-8")
        count = sum(1 for line in kept.split("\n") if line.startswith("  ") and not line.startswith("    ") and line.strip())
        return f"Core did not answer /api/v1/sd/snmp-auths — kept {count} per-asset SNMP auth(s) already in snmp.yml."
    new = splice(text, yaml_block(auths))
    changed = new != text
    if changed:
        path.write_text(new, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    status = f"Per-asset SNMP auths in snmp.yml: {len(auths)}{'' if changed else ' (unchanged)'}."
    if changed and reload:
        status += " snmp_exporter reloaded." if reload_exporter() else " snmp_exporter not reloaded — docker compose restart snmp-exporter."
    return status


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
