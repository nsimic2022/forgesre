"""Where the built new UI lives, and whether it is there."""

from __future__ import annotations

import os
from pathlib import Path


def web_dist_dir() -> Path:
    """Built new UI (web/dist). WEB_DIST in the container, the repo folder in development."""
    return Path(os.environ.get("WEB_DIST") or (Path(__file__).resolve().parents[2] / "web" / "dist"))


def web_ui_available() -> bool:
    return (web_dist_dir() / "index.html").is_file()
