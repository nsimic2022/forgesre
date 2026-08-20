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

    def get_name(self) -> str:
        return "openai-compatible"

    def get_model(self) -> str:
        return self.model

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        endpoint = self.url
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": 700,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(endpoint, json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
            return extract_json(content)
        except Exception:
            return None


def make_provider(url: str | None, model: str = "local", timeout: float = 180.0) -> LLMProvider:
    if not url:
        return NullLLM()
    return OpenAICompatibleLLM(url, model=model, timeout=timeout)


def extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
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
