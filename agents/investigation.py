"""ForgeSRE Investigation Agent.

Read-only: receives already-collected incident data and returns a structured RCA.
It never gets SSH, Docker, or infrastructure write credentials.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

DISCLAIMER = "AI has not modified the system."


def investigate(
    context: dict[str, Any],
    llm_url: str | None = None,
    llm_model: str = "local",
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Return summary, likely_cause, evidence, confidence, recommended_action."""
    heuristic = _heuristic(context)
    if llm_url:
        llm_result = _llm(context, llm_url, llm_model, timeout)
        if llm_result:
            llm_result["provider"] = "llm"
            llm_result["disclaimer"] = DISCLAIMER
            llm_result.setdefault("evidence", heuristic.get("evidence") or [])
            return llm_result
        heuristic["provider_note"] = "LLM unreachable; used builtin analyst on real evidence."
    heuristic["provider"] = "builtin-analyst"
    heuristic["disclaimer"] = DISCLAIMER
    return heuristic


def _heuristic(context: dict[str, Any]) -> dict[str, Any]:
    incident = context.get("incident") or {}
    asset = context.get("asset") or {}
    alert = context.get("alert") or {}
    metrics = context.get("metrics") or {}
    logs = context.get("logs") or []
    title = str(incident.get("title") or alert.get("alertname") or "Unknown alert")
    hostname = asset.get("hostname") or incident.get("asset") or "unknown host"
    cpu = _num(metrics.get("cpu_percent"))
    disk = _num(metrics.get("disk_percent"))
    load = _num(metrics.get("load"))
    net = _num(metrics.get("network_rx_bytes"))

    evidence: list[str] = []
    if cpu is not None:
        evidence.append(f"CPU is {cpu:.1f}%")
    if disk is not None:
        evidence.append(f"Disk usage is {disk:.1f}%")
    if load is not None:
        evidence.append(f"Load is {load}")
    if logs:
        evidence.append(f"{len(logs)} recent log line(s) attached")
    if alert.get("alertname"):
        evidence.append(f"Alert {alert.get('alertname')} is firing")
    if not evidence:
        evidence.append("Alert payload and asset inventory were available; live metrics were limited.")

    alertname = str(alert.get("alertname") or title)
    if "cpu" in alertname.lower() or (cpu is not None and cpu > 80):
        cause = "High process activity."
        action = "Engineer should inspect top CPU processes."
        summary = f"CPU usage increased rapidly on {hostname}."
        confidence = 82 if cpu and cpu > 80 else 70
        if net is not None:
            evidence.append("Network traffic unchanged or not implicated")
        if disk is not None and disk < 80:
            evidence.append("No filesystem anomaly detected")
    elif "file" in alertname.lower() or "disk" in alertname.lower() or (disk is not None and disk > 80):
        cause = "Filesystem usage above threshold."
        action = "Engineer should verify disk usage, growth, and the owning team."
        summary = f"Disk capacity is under pressure on {hostname}."
        confidence = 85 if disk and disk > 80 else 68
    else:
        cause = f"Alert condition matched for {title}."
        action = "Engineer should inspect evidence, Grafana, and recent changes."
        summary = f"Incident on {hostname}: {title}."
        confidence = 60

    history = context.get("history") or []
    if history:
        evidence.append(f"{len(history)} previous related incident(s)")

    return {
        "summary": summary,
        "likely_cause": cause,
        "confidence": confidence,
        "evidence": evidence,
        "recommended_action": action,
    }


def _llm(context: dict[str, Any], llm_url: str, model: str, timeout: float) -> dict[str, Any] | None:
    url = llm_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    prompt = (
        "You are a read-only SRE investigation assistant. "
        "Never claim you changed infrastructure. "
        "Return ONLY JSON with keys: summary, likely_cause, confidence (0-100), "
        "evidence (array of strings), recommended_action.\n\n"
        f"CONTEXT:\n{json.dumps(context, default=str)[:12000]}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You analyze incidents. You cannot modify systems."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 600,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        if not parsed:
            return None
        confidence = int(parsed.get("confidence") or 0)
        confidence = max(0, min(100, confidence))
        evidence = parsed.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        return {
            "summary": str(parsed.get("summary") or "").strip() or _heuristic(context)["summary"],
            "likely_cause": str(parsed.get("likely_cause") or parsed.get("possible_root_cause") or "").strip(),
            "confidence": confidence,
            "evidence": [str(item) for item in evidence],
            "recommended_action": str(parsed.get("recommended_action") or "").strip(),
        }
    except Exception:
        return None


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
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


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
