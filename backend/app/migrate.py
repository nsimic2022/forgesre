"""Add columns without a heavy migration tool."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "assets" in tables:
        existing = {col["name"] for col in inspector.get_columns("assets")}
        if "source" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN source VARCHAR(32) DEFAULT 'manual'")
        if "netbox_id" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN netbox_id VARCHAR(64) DEFAULT ''")
        if "scrape_address" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN scrape_address VARCHAR(128) DEFAULT ''")
        if "contact_name" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN contact_name VARCHAR(255) DEFAULT ''")
        if "owner_email" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN owner_email VARCHAR(255) DEFAULT ''")
        if "owner_phone" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN owner_phone VARCHAR(64) DEFAULT ''")
        if "notes" not in existing:
            statements.append("ALTER TABLE assets ADD COLUMN notes TEXT DEFAULT ''")
    if "evidence" in tables:
        existing = {col["name"] for col in inspector.get_columns("evidence")}
        mapping = {
            "evidence_id": "ALTER TABLE evidence ADD COLUMN evidence_id VARCHAR(64) DEFAULT ''",
            "source": "ALTER TABLE evidence ADD COLUMN source VARCHAR(64) DEFAULT ''",
            "query": "ALTER TABLE evidence ADD COLUMN query TEXT DEFAULT ''",
            "asset_ref": "ALTER TABLE evidence ADD COLUMN asset_ref VARCHAR(64) DEFAULT ''",
            "hash": "ALTER TABLE evidence ADD COLUMN hash VARCHAR(64) DEFAULT ''",
            "confidence": "ALTER TABLE evidence ADD COLUMN confidence FLOAT DEFAULT 1.0",
        }
        for name, sql in mapping.items():
            if name not in existing:
                statements.append(sql)
    if "investigations" in tables:
        existing = {col["name"] for col in inspector.get_columns("investigations")}
        mapping = {
            "result": "ALTER TABLE investigations ADD COLUMN result JSON",
            "engine": "ALTER TABLE investigations ADD COLUMN engine VARCHAR(64) DEFAULT 'forgerca'",
            "engine_version": "ALTER TABLE investigations ADD COLUMN engine_version VARCHAR(32) DEFAULT '0.3.0'",
            "model": "ALTER TABLE investigations ADD COLUMN model VARCHAR(128) DEFAULT ''",
            "requested_by": "ALTER TABLE investigations ADD COLUMN requested_by VARCHAR(255) DEFAULT ''",
        }
        for name, sql in mapping.items():
            if name not in existing:
                statements.append(sql)
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))
        if "evidence" in tables:
            conn.execute(
                text("UPDATE evidence SET evidence_id = 'EV-LEGACY-' || id WHERE evidence_id IS NULL OR evidence_id = ''")
            )
        if "investigations" in tables:
            if engine.dialect.name == "postgresql":
                conn.execute(text("UPDATE investigations SET result = '{}'::json WHERE result IS NULL"))
            else:
                conn.execute(text("UPDATE investigations SET result = '{}' WHERE result IS NULL"))
