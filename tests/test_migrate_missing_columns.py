"""An update must add new model columns to existing tables (a missing forgeai_chats.mode gave 500s)."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.db import Base
from app.migrate import _add_missing_model_columns, migrate


def test_old_chat_table_gets_the_mode_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        # the table as it was before the chat mode existed
        conn.execute(text("DROP TABLE forgeai_chats"))
        conn.execute(text("CREATE TABLE forgeai_chats (id INTEGER PRIMARY KEY, owner VARCHAR(255), title VARCHAR(255), "
                          "incident_id INTEGER, created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("INSERT INTO forgeai_chats (id, owner, title) VALUES (1, 'a@dc.local', 'old chat')"))
    migrate(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("forgeai_chats")}
    assert "mode" in cols
    with engine.connect() as conn:
        assert conn.execute(text("SELECT mode FROM forgeai_chats WHERE id = 1")).scalar() == ""


def test_safety_net_adds_any_plain_column_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'net.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tmp_push AS SELECT id, email, endpoint, p256dh, auth, created_at FROM push_subscriptions"))
        conn.execute(text("DROP TABLE push_subscriptions"))
        conn.execute(text("ALTER TABLE tmp_push RENAME TO push_subscriptions"))
    added = _add_missing_model_columns(engine)
    assert any("push_subscriptions ADD COLUMN fails" in s and "DEFAULT 0" in s for s in added)
    assert _add_missing_model_columns(engine) == []
