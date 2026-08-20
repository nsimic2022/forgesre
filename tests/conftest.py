"""Pytest bootstrap: sqlite, no Docker required."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'test.db'}")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FORGESRE_ADMIN_EMAIL", "admin@forgesre.local")
os.environ.setdefault("FORGESRE_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("ALERTMANAGER_WEBHOOK_TOKEN", "forgesre-dev-webhook-token")
os.environ.setdefault("FORGESRE_LOG_FILE", "")
os.environ.setdefault("FRONTEND_DIR", str(ROOT / "frontend"))
os.environ.setdefault("PROMETHEUS_URL", "http://127.0.0.1:9")
os.environ.setdefault("LOKI_URL", "http://127.0.0.1:9")

sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "agents"))

(ROOT / "data").mkdir(exist_ok=True)
db_path = ROOT / "data" / "test.db"
if db_path.exists():
    db_path.unlink()
