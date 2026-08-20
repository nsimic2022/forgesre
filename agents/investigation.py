"""ForgeSRE Investigation Agent (V0.1 wrapper).

Read-only: receives already-collected incident data and returns a structured RCA.
It never gets SSH, Docker, or infrastructure write credentials.

V0.3: implementation is ForgeRCA. This module keeps the V0.1 function signature.
"""

from __future__ import annotations

from typing import Any

from rca.engines import DISCLAIMER, ForgeRCA
from rca.llm import make_provider

__all__ = ["DISCLAIMER", "investigate"]


def investigate(
    context: dict[str, Any],
    llm_url: str | None = None,
    llm_model: str = "local",
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Return summary, likely_cause, evidence, confidence, recommended_action."""
    engine = ForgeRCA(llm=make_provider(llm_url, llm_model, timeout))
    return engine.investigate(context)
