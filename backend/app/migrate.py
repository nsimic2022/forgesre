"""Add columns without a heavy migration tool."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "users" in tables:
        existing = {col["name"] for col in inspector.get_columns("users")}
        if "journal_error_ack_id" not in existing:
            statements.append("ALTER TABLE users ADD COLUMN journal_error_ack_id INTEGER DEFAULT 0")
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
        if "jobs" not in tables:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS jobs ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "kind VARCHAR(64), status VARCHAR(32), object_type VARCHAR(64), "
                    "object_id VARCHAR(64), payload JSON, error TEXT, attempts INTEGER, "
                    "created_at DATETIME, started_at DATETIME, finished_at DATETIME)"
                    if engine.dialect.name == "sqlite"
                    else "CREATE TABLE IF NOT EXISTS jobs ("
                    "id SERIAL PRIMARY KEY, "
                    "kind VARCHAR(64), status VARCHAR(32), object_type VARCHAR(64), "
                    "object_id VARCHAR(64), payload JSON, error TEXT, attempts INTEGER DEFAULT 0, "
                    "created_at TIMESTAMPTZ, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ)"
                )
            )
        if "incident_notes" not in tables:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS incident_notes ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "incident_id INTEGER, at DATETIME, actor VARCHAR(255), body TEXT)"
                    if engine.dialect.name == "sqlite"
                    else "CREATE TABLE IF NOT EXISTS incident_notes ("
                    "id SERIAL PRIMARY KEY, "
                    "incident_id INTEGER REFERENCES incidents(id), "
                    "at TIMESTAMPTZ, actor VARCHAR(255), body TEXT)"
                )
            )
        if "incidents" in tables:
            existing = {col["name"] for col in inspector.get_columns("incidents")}
            extras = {
                "resolved_by": "ALTER TABLE incidents ADD COLUMN resolved_by VARCHAR(255) DEFAULT ''",
                "resolved_at": "ALTER TABLE incidents ADD COLUMN resolved_at TIMESTAMPTZ"
                if engine.dialect.name == "postgresql"
                else "ALTER TABLE incidents ADD COLUMN resolved_at DATETIME",
            }
            for name, sql in extras.items():
                if name not in existing:
                    conn.execute(text(sql))
            number_col = next((col for col in inspector.get_columns("incidents") if col["name"] == "number"), None)
            if number_col is not None and engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE incidents ALTER COLUMN number TYPE VARCHAR(64)"))
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
        if "scheduled_reports" not in tables:
            if engine.dialect.name == "sqlite":
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS scheduled_reports ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "name VARCHAR(255), to_email VARCHAR(255), interval_hours INTEGER DEFAULT 6, "
                        "asset_ids JSON, enabled BOOLEAN DEFAULT 1, "
                        "last_run_at DATETIME, next_run_at DATETIME, "
                        "created_by VARCHAR(255) DEFAULT '', created_at DATETIME)"
                    )
                )
            else:
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS scheduled_reports ("
                        "id SERIAL PRIMARY KEY, "
                        "name VARCHAR(255), to_email VARCHAR(255), interval_hours INTEGER DEFAULT 6, "
                        "asset_ids JSON, enabled BOOLEAN DEFAULT TRUE, "
                        "last_run_at TIMESTAMPTZ, next_run_at TIMESTAMPTZ, "
                        "created_by VARCHAR(255) DEFAULT '', created_at TIMESTAMPTZ)"
                    )
                )
        if "mail_contacts" not in tables:
            if engine.dialect.name == "sqlite":
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS mail_contacts ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "email VARCHAR(255) UNIQUE, name VARCHAR(255) DEFAULT '', "
                        "created_by VARCHAR(255) DEFAULT '', created_at DATETIME)"
                    )
                )
            else:
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS mail_contacts ("
                        "id SERIAL PRIMARY KEY, "
                        "email VARCHAR(255) UNIQUE, name VARCHAR(255) DEFAULT '', "
                        "created_by VARCHAR(255) DEFAULT '', created_at TIMESTAMPTZ)"
                    )
                )
