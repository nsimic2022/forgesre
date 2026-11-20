"""Local embeddings for document search: one call to the llama.cpp server started with --embedding.

OpenAI-compatible ``POST {url}/embeddings``. nomic-embed-text wants a task prefix on every text
("search_document: " when indexing, "search_query: " when searching). Short timeouts: indexing runs
in the doc_index job, searching inside a ForgeAI job or a single request (one short text).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.settings import settings

PREFIX = {"document": "search_document: ", "query": "search_query: "}


class EmbeddingError(RuntimeError):
    pass


def model_name() -> str:
    return settings.embed_model


def available() -> bool:
    return bool(settings.embed_url)


def embed(texts: list[str], *, kind: str = "document", timeout: float | None = None) -> list[list[float]]:
    """One vector per text, in order. Raises EmbeddingError when off, unreachable or malformed."""
    url = settings.embed_url
    if not url:
        raise EmbeddingError("document search needs ForgeAI on (ai.enabled) and the embedding server (ai.embed)")
    prefix = PREFIX.get(kind, "") if "nomic" in model_name().lower() else ""
    body: dict[str, Any] = {"input": [prefix + t for t in texts], "model": model_name()}
    try:
        reply = httpx.post(f"{url.rstrip('/')}/embeddings", json=body, timeout=timeout or (60.0 if kind == "document" else 10.0))
        reply.raise_for_status()
        data = reply.json().get("data") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise EmbeddingError(f"embedding server at {url}: {exc}") from exc
    vectors = [row.get("embedding") for row in sorted(data, key=lambda r: r.get("index", 0))]
    if len(vectors) != len(texts) or not all(isinstance(v, list) and v for v in vectors):
        raise EmbeddingError("the embedding server returned a different number of vectors")
    return [[float(x) for x in v] for v in vectors]
