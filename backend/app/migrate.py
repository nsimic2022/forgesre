"""Add V0.2 columns without a heavy migration tool."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    if "assets" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("assets")}
    statements = []
    if "source" not in existing:
        statements.append("ALTER TABLE assets ADD COLUMN source VARCHAR(32) DEFAULT 'manual'")
    if "netbox_id" not in existing:
        statements.append("ALTER TABLE assets ADD COLUMN netbox_id VARCHAR(64) DEFAULT ''")
    if "scrape_address" not in existing:
        statements.append("ALTER TABLE assets ADD COLUMN scrape_address VARCHAR(128) DEFAULT ''")
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))
