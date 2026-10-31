"""Async llama.cpp client: AsyncClient complete + SSE stream."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from rca.llm import OpenAICompatibleLLM, STREAM_CONNECT_TIMEOUT, _sse_token

ROOT = Path(__file__).resolve().parents[1]


def test_llm_module_uses_async_client():
    src = (ROOT / "agents" / "rca" / "llm.py").read_text(encoding="utf-8")
    assert "AsyncClient" in src
    assert "client.stream" in src
    assert "httpx.Client(" not in src
    assert STREAM_CONNECT_TIMEOUT == 5.0


def test_sse_token_parses_delta_and_done():
    assert _sse_token('data: {"choices":[{"delta":{"content":"CPU"}}]}') == "CPU"
    assert _sse_token("data: [DONE]") == "[DONE]"
    assert _sse_token("") is None
    assert _sse_token("event: ping") is None


def test_complete_json_async_uses_async_client(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen.gguf"}]})
        body = json.loads(request.content)
        assert body.get("stream") is not True
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"summary": "stream-ok", "likely_cause": "c", "recommended_action": "a"}'
                        }
                    }
                ]
            },
        )

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("rca.llm.httpx.AsyncClient", fake_client)
    llm = OpenAICompatibleLLM("http://127.0.0.1:8088/v1", timeout=8.0)
    out = asyncio.run(llm.complete_json_async("sys", "user"))
    assert out is not None
    assert out["summary"] == "stream-ok"
    assert llm.complete_json("sys", "user")["summary"] == "stream-ok"


def test_stream_completion_yields_tokens(monkeypatch):
    chunks = (
        b'data: {"choices":[{"delta":{"content":"Root"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" cause"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen.gguf"}]})
        body = json.loads(request.content)
        assert body.get("stream") is True
        assert str(request.url).endswith("/chat/completions")
        return httpx.Response(200, content=chunks)

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("rca.llm.httpx.AsyncClient", fake_client)

    async def _run():
        llm = OpenAICompatibleLLM("http://127.0.0.1:8088/v1", timeout=8.0)
        parts: list[str] = []
        async for token in llm.stream_completion("sys", "what failed"):
            parts.append(token)
        return parts

    assert asyncio.run(_run()) == ["Root", " cause"]
