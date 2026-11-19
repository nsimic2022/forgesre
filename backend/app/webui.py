"""Where the built new UI lives, and whether it is there."""

from __future__ import annotations

import os
from pathlib import Path


def web_dist_dir() -> Path:
    """Built new UI (web/dist). WEB_DIST in the container, the repo folder in development."""
    return Path(os.environ.get("WEB_DIST") or (Path(__file__).resolve().parents[2] / "web" / "dist"))


def web_ui_available() -> bool:
    return (web_dist_dir() / "index.html").is_file()


def ui_home() -> str:
    """Which UI opens at /: "new" (default when the new UI is built) or "classic" (ui.home in forgesre.yml)."""
    from app.settings import settings

    value = str(((settings.yaml or {}).get("ui") or {}).get("home") or "new").strip().lower()
    return "classic" if value == "classic" else "new"


def home_redirect() -> str | None:
    """Where / should send a signed-in user, or None to render the classic dashboard."""
    if ui_home() == "new" and web_ui_available():
        return "/app/"
    return None
