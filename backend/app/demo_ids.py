"""Demo / lab inventory ids. Stdlib only — host CLI must not import sqlalchemy.

Seeded lab rows use prefix forge-demo- (forge-demo-01, forge-demo-win-01, …).
ORM seed data stays in app.seed; this module is the prefix helper only.
"""

from __future__ import annotations

DEMO_ASSET_PREFIX = "forge-demo-"


def is_demo_asset_id(value: str | None) -> bool:
    """True for seeded lab inventory (forge-demo-01, forge-demo-win-01, forge-demo-sw-01, …)."""
    return str(value or "").strip().lower().startswith(DEMO_ASSET_PREFIX)
