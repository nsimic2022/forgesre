"""LLM providers. ForgeSRE never imports a cloud SDK as a hard dependency."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx

from rca.sanitize import sanitize

SHELLISH = ("sudo ", "rm -", "ssh ", "docker ", "iptables", "mkfs", "dd if=", "systemctl ", "reboot")


class LLMProvider(Protocol):
    def get_name(self) -> str: ...
    def get_model(self) -> str: ...
    def complete_json(self, system: str, user: str) -> dict[str, Any] | None: ...


class NullLLM:
    last_error = ""

    def get_name(self) -> str:
        return "none"

    def get_model(self) -> str:
        return ""

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        return None


class OpenAICompatibleLLM:
    def __init__(self, url: str, model: str = "local", timeout: float = 180.0) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.last_error = ""

    def get_name(self) -> str:
        return "openai-compatible"

    def get_model(self) -> str:
        return self.model

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        self.last_error = ""
        endpoint = self.url
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + "/chat/completions"
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
                model = self._resolve_model(client)
                last_why = ""
                # Qwen2.5 (bundled GGUF) wants a plain OpenAI body. Extra
                # chat_template_kwargs can 400 on llama.cpp, which we used to
                # swallow as "LLM unreachable".
                for extra in ({}, {"chat_template_kwargs": {"enable_thinking": False}}):
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 512,
                    }
                    payload.update(extra)
                    response = client.post(endpoint, json=payload)
                    if response.status_code in {400, 404, 422}:
                        last_why = f"HTTP {response.status_code}"
                        continue
                    response.raise_for_status()
                    choice = (response.json().get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    content = message.get("content") or message.get("reasoning_content") or choice.get("text") or ""
                    parsed = extract_json(content)
                    if parsed:
                        self.model = model
                        return parsed
                    last_why = "LLM returned text that was not JSON"
                self.last_error = last_why or "LLM unreachable"
                return None
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return None

    def _resolve_model(self, client: httpx.Client) -> str:
        configured = (self.model or "local").strip() or "local"
        if configured not in {"local", "default"}:
            return configured
        try:
            response = client.get(self.url.rstrip("/") + "/models")
            response.raise_for_status()
            rows = response.json().get("data") or []
            if rows and rows[0].get("id"):
                return str(rows[0]["id"])
        except Exception:
            pass
        return configured


def make_provider(url: str | None, model: str = "local", timeout: float = 180.0) -> LLMProvider:
    if not url:
        return NullLLM()
    return OpenAICompatibleLLM(url, model=model, timeout=timeout)


def extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def prompt_context(context: dict[str, Any]) -> str:
    compact = sanitize(context)
    raw = json.dumps(compact, default=str)
    return raw[:12000]


def validate_recommendation(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return "Engineer should inspect evidence and the matched playbook. AI has not modified the system."
    lowered = cleaned.lower()
    if any(token in lowered for token in SHELLISH):
        return f"RECOMMENDED ACTION (not executed): {cleaned}"
    if cleaned.lower().startswith("action executed"):
        return "RECOMMENDED ACTION (not executed): " + cleaned.split(":", 1)[-1].strip()
    return cleaned
