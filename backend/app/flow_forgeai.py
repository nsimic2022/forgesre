"""ForgeSRE → ForgeAI model: the local llama.cpp server, the optional cloud model, and how recent
questions went (registered in app.flows).

Local: on → DNS/TCP → /models (which model is loaded) → the last 24 h of calls (used, rejected by
the checks, errors). Cloud (opt-in): settings only; the check never sends anything to a cloud model.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.flows import Flow, http_step, network_steps, register, step
from app.models import ForgeAiCall, utcnow
from app.settings import settings


def _local(db: Session) -> list[dict[str, Any]]:
    if not settings.ai_enabled or not settings.llm_url:
        return [step("on", "ForgeAI", "off", "off", "ForgeAI is off.", fix="Settings › RCA / AI: turn ForgeAI on")]
    url = settings.llm_url.rstrip("/")
    steps = [step("on", "ForgeAI", settings.llm_model or "local", "ok", f"Asks {url} (timeout {settings.llm_timeout} s).")]
    net, ok = network_steps(url, what="the model server")
    steps += net
    if ok:
        models, data = http_step("model", "Model", url + "/models", ok_text="The model server answers.",
                                 fix="docker compose logs llm; ./forgesre fetch-llm if no model was downloaded")
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if rows:
            models["sub"] = str(rows[0].get("id") or "")[:40]
            models["detail"] = f"Loaded: {rows[0].get('id')}."
        elif models["status"] == "ok":
            models.update(status="warn", detail="The server answers but no model is loaded.", fix="./forgesre fetch-llm")
        steps.append(models)
    since = utcnow() - timedelta(days=1)
    calls = db.query(ForgeAiCall).filter(ForgeAiCall.created_at >= since).order_by(ForgeAiCall.id.desc()).all()
    bad = [c for c in calls if c.status == "error"]
    rejected = [c for c in calls if c.status == "rejected"]
    rows = [{"at": c.created_at.isoformat() if c.created_at else "", "status": c.status, "error": (c.reason or "")[:160],
             "subject": f"{c.task} · {c.skill or '-'} · {c.duration_ms or 0} ms"} for c in (bad + rejected + calls)[:15]]
    steps.append(step("calls", "Answers", f"{len(calls)} in 24 h", "unknown" if not calls else ("bad" if bad and len(bad) == len(calls) else ("warn" if bad else "ok")),
                      (f"{len(calls)} question(s): {sum(c.status == 'used' for c in calls)} used, {len(rejected)} rejected by the checks, {len(bad)} failed."
                       if calls else "No question in the last 24 h."), rows=rows,
                      fix="Failed calls: the model timed out or crashed (docker compose logs llm). Rejected answers are the checks working." if bad else ""))
    return steps


def _cloud() -> list[dict[str, Any]]:
    from app.cloud_llm import info

    cloud = info()
    if not cloud["enabled"]:
        return [step("cloud", "Cloud model", "off", "off", "No cloud model (default): data stays on this server.")]
    return [step("cloud", "Cloud model", f"{cloud['provider']} · {cloud['model']}", "ok" if cloud["ready"] else "bad",
                 (f"Engineers can re-run an answer on {cloud['host'] or cloud['provider']}; incident data then leaves this server."
                  if cloud["ready"] else f"Not ready: {cloud['why']}."), fix="" if cloud["ready"] else "docs/install-guide.md › 8. ForgeAI: local model, embeddings, cloud and MCP")]


def check(db: Session) -> dict[str, Any]:
    """The local model path and its recent answers; the cloud model's settings."""
    return {"lanes": [{"id": "local", "title": "ForgeSRE → local model (llama.cpp)", "steps": _local(db)},
                      {"id": "cloud", "title": "Cloud model (opt-in, never called by this check)", "steps": _cloud()}]}


register(Flow(name="forgeai", label="ForgeAI model", check=check, configured=lambda: bool(settings.ai_enabled and settings.llm_url),
              log_words=("forgeai", "llm"), services=("core", "llm"),
              about="How ForgeAI questions reach the model: the local llama.cpp server, its loaded model, recent answers, and the optional cloud model."))
