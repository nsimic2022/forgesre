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


# LLM rewrite only. Builtin ForgeRCA keeps full evidence in the incident result.
# 12000 chars was ~3–6k tokens (CPU 4B prefill of ~6000 tok ≈ 4 min, then cancel).
PROMPT_CONTEXT_MAX_CHARS = 5000
_MAX_FACTS = 12
_MAX_METRICS = 12
_MAX_LOGS = 8
_MAX_LOG_CHARS = 160
_MAX_ANOMALIES = 8
_MAX_HYPOTHESES = 5
_MAX_LIMITATIONS = 8
_UNIX_TS_MIN = 1_000_000_000  # Prom sample timestamps, not CPU/mem percents


def prompt_context(
    context: dict[str, Any],
    *,
    facts: list[dict[str, Any]] | None = None,
    hypotheses: list[dict[str, Any]] | None = None,
    draft: dict[str, Any] | None = None,
    max_chars: int | None = None,
) -> str:
    """Compact, sanitized JSON for the LLM user message — not a Prom/Loki dump.

    Builtin investigation still stores full facts, anomalies, evidence IDs, and
    PromQL on the incident. This string is only what ``complete_json`` sees.
    """
    cap = PROMPT_CONTEXT_MAX_CHARS if max_chars is None else max(500, int(max_chars))
    cleaned = sanitize(context if isinstance(context, dict) else {})
    payload = _rewrite_payload(
        cleaned,
        facts=facts,
        hypotheses=hypotheses,
        draft=draft,
    )
    payload = drop_prom_blobs(payload)
    raw = _dump(payload)
    if len(raw) <= cap:
        return raw
    for key in ("logs", "hypotheses", "anomalies"):
        if key in payload:
            payload.pop(key)
            raw = _dump(payload)
            if len(raw) <= cap:
                return raw
    facts_rows = payload.get("facts")
    if isinstance(facts_rows, list) and len(facts_rows) > 6:
        payload["facts"] = facts_rows[:6]
        raw = _dump(payload)
        if len(raw) <= cap:
            return raw
    return raw[:cap]


def drop_prom_blobs(value: Any) -> Any:
    """Strip Prometheus matrices / instant vectors (``values: [[ts, x], …]``)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "value" and _is_prom_values(item):
                out[key] = _prom_scalar(item)
                continue
            if lowered == "values" and _is_prom_values(item):
                continue
            if lowered in {"result", "data"} and _is_prom_api_block(item):
                continue
            if lowered == "metric" and _is_prom_labelset(item):
                continue
            dropped = drop_prom_blobs(item)
            if dropped is not None:
                out[key] = dropped
        return out
    if isinstance(value, list):
        if _is_prom_values(value):
            return None
        cleaned = []
        for item in value:
            if isinstance(item, dict) and _is_prom_series(item):
                continue
            dropped = drop_prom_blobs(item)
            if dropped is not None:
                cleaned.append(dropped)
        return cleaned
    return value


def _rewrite_payload(
    context: dict[str, Any],
    *,
    facts: list[dict[str, Any]] | None,
    hypotheses: list[dict[str, Any]] | None,
    draft: dict[str, Any] | None,
) -> dict[str, Any]:
    incident = context.get("incident") if isinstance(context.get("incident"), dict) else {}
    asset = context.get("asset") if isinstance(context.get("asset"), dict) else {}
    payload: dict[str, Any] = {}
    header = {
        key: incident[key]
        for key in ("number", "title", "severity")
        if incident.get(key) not in (None, "")
    }
    host = asset.get("hostname") or incident.get("asset")
    if host:
        header["host"] = host
    if header:
        payload["incident"] = header

    alert_names = _alert_names(context)
    if alert_names:
        payload["alerts"] = alert_names

    fact_texts = _fact_texts(facts if facts is not None else context.get("facts"))
    if fact_texts:
        payload["facts"] = fact_texts

    metrics = _metric_snapshots(context)
    if metrics:
        payload["metrics"] = metrics

    logs = _log_snippets(context)
    if logs:
        payload["logs"] = logs

    hyp_rows = _hypothesis_rows(hypotheses if hypotheses is not None else context.get("hypotheses"))
    if hyp_rows:
        payload["hypotheses"] = hyp_rows

    anomalies = _anomaly_texts(context.get("anomalies"))
    if anomalies:
        payload["anomalies"] = anomalies

    playrule = _playrule_row(context.get("playrules"))
    if playrule:
        payload["playrule"] = playrule

    limitations = [
        str(item)[:200]
        for item in (context.get("limitations") or [])
        if item
    ][:_MAX_LIMITATIONS]
    if limitations:
        payload["limitations"] = limitations

    if isinstance(draft, dict):
        packed = {
            key: str(draft[key]).strip()[:400]
            for key in ("summary", "likely_cause", "recommended_action")
            if str(draft.get(key) or "").strip()
        }
        if packed:
            payload["draft"] = packed
    return payload


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


def _is_unix_ts(value: Any) -> bool:
    try:
        return float(value) >= _UNIX_TS_MIN
    except (TypeError, ValueError):
        return False


def _is_prom_sample(item: Any) -> bool:
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        return False
    if not _is_unix_ts(item[0]):
        return False
    try:
        float(item[1])
        return True
    except (TypeError, ValueError):
        return False


def _is_prom_values(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if _is_prom_sample(value) and not isinstance(value[0], (list, tuple)):
        return True
    head = value[: min(4, len(value))]
    return bool(head) and all(_is_prom_sample(row) for row in head)


def _prom_scalar(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == 2 and _is_prom_sample(value):
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return value[1]
    return value


def _is_prom_labelset(item: Any) -> bool:
    return isinstance(item, dict) and ("__name__" in item or "instance" in item)


def _is_prom_series(item: dict[str, Any]) -> bool:
    return "metric" in item and ("values" in item or "value" in item)


def _is_prom_api_block(item: Any) -> bool:
    if isinstance(item, list) and item and isinstance(item[0], dict) and _is_prom_series(item[0]):
        return True
    if isinstance(item, dict):
        if item.get("resultType") in {"matrix", "vector", "scalar"}:
            return True
        inner = item.get("result")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict) and _is_prom_series(inner[0]):
            return True
    return False


def _alert_names(context: dict[str, Any]) -> list[str]:
    names: list[str] = []
    rows = context.get("alerts")
    if not rows:
        alert = context.get("alert")
        rows = [alert] if alert else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("alertname") or row.get("name")
        if name and str(name) not in names:
            names.append(str(name))
    return names[:6]


def _fact_texts(facts: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(facts, list):
        return texts
    for item in facts:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
        else:
            text = str(item).strip()
        if text:
            texts.append(text[:240])
        if len(texts) >= _MAX_FACTS:
            break
    return texts


def _metric_unit(name: str, unit: Any) -> str:
    if unit:
        return str(unit)
    if "percent" in name or name.endswith("_usage"):
        return "percent"
    return ""


def _add_metric(found: list[dict[str, Any]], seen: set[str], name: Any, value: Any, unit: Any = "") -> None:
    label = str(name or "").strip()
    if not label or label in seen or label in {"queries", "error", "status", "data", "raw"}:
        return
    value = _prom_scalar(value)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return
    seen.add(label)
    row: dict[str, Any] = {"name": label, "value": numeric}
    unit_s = _metric_unit(label, unit)
    if unit_s:
        row["unit"] = unit_s
    found.append(row)


def _metric_snapshots(context: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    metrics = context.get("metrics")
    if isinstance(metrics, dict):
        for key, raw in metrics.items():
            if isinstance(raw, dict) and "value" in raw:
                _add_metric(found, seen, raw.get("name") or key, raw.get("value"), raw.get("unit"))
            else:
                _add_metric(found, seen, key, raw)
    for item in context.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("kind") or "").upper()
        content = item.get("content")
        if kind not in {"METRIC", "METRICS"} and not (isinstance(content, dict) and content.get("type") == "metric"):
            continue
        if isinstance(content, dict):
            _add_metric(found, seen, content.get("name"), content.get("value"), content.get("unit"))
        if len(found) >= _MAX_METRICS:
            break
    return found[:_MAX_METRICS]


def _log_snippets(context: dict[str, Any]) -> list[str]:
    snippets: list[str] = []

    def add(text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            snippets.append(cleaned[:_MAX_LOG_CHARS])

    for line in context.get("logs") or []:
        add(line if isinstance(line, str) else str(line))
        if len(snippets) >= _MAX_LOGS:
            return snippets
    for item in context.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("kind") or "").upper()
        if kind != "LOG":
            continue
        content = item.get("content")
        if isinstance(content, dict):
            msg = str(content.get("message") or content.get("line") or "")
            sev = str(content.get("severity") or "").strip()
            add(f"{sev}: {msg}" if sev else msg)
        elif content:
            add(str(content))
        if len(snippets) >= _MAX_LOGS:
            break
    return snippets[:_MAX_LOGS]


def _hypothesis_rows(hypotheses: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(hypotheses, list):
        return rows
    for item in hypotheses:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        packed: dict[str, Any] = {"summary": summary[:200]}
        if item.get("id"):
            packed["id"] = item["id"]
        if item.get("confidence") is not None:
            packed["confidence"] = item["confidence"]
        rows.append(packed)
        if len(rows) >= _MAX_HYPOTHESES:
            break
    return rows


def _anomaly_texts(anomalies: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(anomalies, list):
        return texts
    for item in anomalies:
        if isinstance(item, dict):
            text = str(item.get("summary") or "").strip()
        else:
            text = str(item).strip()
        if text:
            texts.append(text[:200])
        if len(texts) >= _MAX_ANOMALIES:
            break
    return texts


def _playrule_row(playrules: Any) -> dict[str, Any] | None:
    if not isinstance(playrules, list) or not playrules:
        return None
    rule = playrules[0]
    if not isinstance(rule, dict):
        return None
    packed = {
        key: rule[key]
        for key in ("name", "playbook")
        if rule.get(key)
    }
    return packed or None


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
