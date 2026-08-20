"""Pytest bootstrap: sqlite, no Docker required."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = f"sqlite:///{ROOT / 'data' / 'test.db'}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["FORGESRE_ADMIN_EMAIL"] = "admin@forgesre.local"
os.environ["FORGESRE_ADMIN_PASSWORD"] = "testpass"
os.environ["ALERTMANAGER_WEBHOOK_TOKEN"] = "forgesre-dev-webhook-token"
os.environ["FORGESRE_LOG_FILE"] = ""
os.environ["FRONTEND_DIR"] = str(ROOT / "frontend")
os.environ["PROMETHEUS_URL"] = "http://127.0.0.1:9"
os.environ["LOKI_URL"] = "http://127.0.0.1:9"

sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "agents"))

(ROOT / "data").mkdir(exist_ok=True)
db_path = ROOT / "data" / "test.db"
if db_path.exists():
    db_path.unlink()
