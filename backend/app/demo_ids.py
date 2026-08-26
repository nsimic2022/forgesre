"""Demo / lab inventory ids. Stdlib only — host CLI must not import sqlalchemy.

Seeded lab rows use prefix forge-demo- (forge-demo-01, forge-demo-win-01, …).
The discovery Approve seed 10.20.30.41 (asset disc-10-20-30-41) is the same class:
lab only, not a real VM, never proof of a scrape.

ORM seed data stays in app.seed; this module is the id/IP helper only.
"""

from __future__ import annotations

from typing import Any

DEMO_ASSET_PREFIX = "forge-demo-"
DEMO_CANDIDATE_IP = "10.20.30.41"
DEMO_CANDIDATE_ASSET_ID = "disc-" + DEMO_CANDIDATE_IP.replace(".", "-")


def is_demo_asset_id(value: str | None) -> bool:
    """True for seeded lab inventory (forge-demo-01, forge-demo-win-01, forge-demo-sw-01, …)."""
    return str(value or "").strip().lower().startswith(DEMO_ASSET_PREFIX)


def _host_token(value: str | None) -> str:
    """Bare IPv4 host from an IP or scrape_address (10.20.30.41:9100 → 10.20.30.41)."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "/" in text:
        text = text.split("/", 1)[0]
    if text.startswith("[") and "]" in text:
        return text[1:].split("]", 1)[0]
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host
    return text


def is_demo_candidate_host(value: str | None) -> bool:
    """True for the seeded discovery IP 10.20.30.41 (optionally with :port)."""
    return _host_token(value) == DEMO_CANDIDATE_IP


def is_lab_inventory(
    asset_id: str | None = None,
    hostname: str | None = None,
    ip: str | None = None,
    scrape_address: str | None = None,
) -> bool:
    """True for forge-demo-* rows and the discovery seed 10.20.30.41 / disc-10-20-30-41."""
    if is_demo_asset_id(asset_id) or is_demo_asset_id(hostname):
        return True
    if is_demo_candidate_host(ip) or is_demo_candidate_host(scrape_address):
        return True
    slug = str(asset_id or hostname or "").strip().lower()
    return slug == DEMO_CANDIDATE_ASSET_ID


def is_lab_inventory_row(item: dict[str, Any] | Any) -> bool:
    if isinstance(item, dict):
        return is_lab_inventory(
            asset_id=item.get("asset_id"),
            hostname=item.get("hostname"),
            ip=item.get("ip"),
            scrape_address=item.get("scrape_address"),
        )
    return is_lab_inventory(
        asset_id=getattr(item, "asset_id", None),
        hostname=getattr(item, "hostname", None),
        ip=getattr(item, "ip", None),
        scrape_address=getattr(item, "scrape_address", None),
    )
