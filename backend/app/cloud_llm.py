"""Optional paid cloud model for ForgeAI (OpenAI, Anthropic, Azure OpenAI or any OpenAI-compatible API).

Off by default: data must not leave the server unless someone chooses so. When ``ai.cloud.enabled``
is on and ``CLOUD_LLM_API_KEY`` is set, an Engineer (permission ``ai_cloud``) may tick "cloud model"
for one question. Automatic runs, Analysts and the morning digest always use the local model.
What goes out is exactly the logged prompt (secrets already removed by ``rca.sanitize``); the call
log records the provider, model and endpoint, and the request is audited.
"""

from __future__ import annotations

from typing import Any

from app.settings import settings


def not_ready() -> str:
    """Why the cloud model cannot be used now, or ''."""
    if not settings.cloud_enabled:
        return "the cloud model is off (ai.cloud.enabled)"
    if not settings.cloud_api_key:
        return "no API key: ./forgesre settings set-secret CLOUD_LLM_API_KEY"
    if not settings.cloud_model:
        return "no model name (ai.cloud.model)"
    if not settings.cloud_url:
        return "no API URL (ai.cloud.url)"
    return ""


def info() -> dict[str, Any]:
    """What the UI may show about the cloud model (never the key)."""
    why = not_ready()
    return {"enabled": settings.cloud_enabled, "ready": not why, "why": why, "provider": settings.cloud_provider,
            "model": settings.cloud_model, "host": settings.cloud_url.split("/")[2] if "://" in settings.cloud_url else ""}


def provider():
    """An LLM provider object for the configured cloud model. Raises LookupError when not ready."""
    from rca.llm import AnthropicLLM, OpenAICompatibleLLM

    why = not_ready()
    if why:
        raise LookupError(why)
    key, model, url, timeout = settings.cloud_api_key, settings.cloud_model, settings.cloud_url, settings.cloud_timeout
    if settings.cloud_provider == "anthropic":
        return AnthropicLLM(key, model, timeout=timeout, url=url)
    if settings.cloud_provider == "azure":
        return OpenAICompatibleLLM(url, model=model, timeout=timeout, api_key=key, key_header="api-key", name="azure-openai")
    return OpenAICompatibleLLM(url, model=model, timeout=timeout, api_key=key, name=settings.cloud_provider)


def check() -> dict[str, str]:
    """Settings › Test: one tiny request with no incident data in it (costs a few tokens)."""
    why = not_ready()
    if why:
        return {"status": "disabled" if not settings.cloud_enabled else "error", "why": why}
    model = provider()
    answer = model.complete_json("Answer with JSON only.", 'Reply with {"ok": true}.', 20)
    if answer is None:
        return {"status": "error", "why": f"{settings.cloud_provider}: {model.last_error or 'no answer'}"}
    return {"status": "ok", "why": f"{settings.cloud_provider} {settings.cloud_model} answered in {model.last_ms} ms"}
