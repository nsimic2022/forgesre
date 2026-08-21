"""RCA engine. ForgeRCA is the production implementation."""

from __future__ import annotations

from typing import Any, Protocol

from rca.analysis import candidate_causes, detect_anomalies, facts_from, metric_samples, score_confidence
from rca.catalog import action_for, classify_family, summary_for
from rca.llm import LLMProvider, NullLLM, prompt_context, validate_recommendation
from rca.types import RCAContext, utc_now

DISCLAIMER = "AI has not modified the system."
FORGERCA_VERSION = "0.3.0"


class RCAEngine(Protocol):
    def get_name(self) -> str: ...
    def get_version(self) -> str: ...
    def get_capabilities(self) -> dict[str, Any]: ...
    def investigate(self, context: RCAContext | dict[str, Any]) -> dict[str, Any]: ...


class ForgeRCA:
    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm or NullLLM()

    def get_name(self) -> str:
        return "forgerca"

    def get_version(self) -> str:
        return FORGERCA_VERSION

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "read_only": True,
            "llm": self.llm.get_name(),
            "code_agent": False,
        }

    def investigate(self, context: RCAContext | dict[str, Any]) -> dict[str, Any]:
        ctx = context if isinstance(context, RCAContext) else RCAContext.from_legacy(context)
        ctx.anomalies = detect_anomalies(ctx)
        hypotheses = candidate_causes(ctx)
        facts = facts_from(ctx)
        sources_ok = not any("unavailable" in item.lower() for item in ctx.limitations)
        confidence = score_confidence(
            anomalies=ctx.anomalies,
            hypotheses=hypotheses,
            history=ctx.historical_incidents,
            maintenance=ctx.maintenance,
            sources_ok=sources_ok,
        )
        top = hypotheses[0] if hypotheses else None
        hostname = ctx.asset.get("hostname") or ctx.incident.get("asset") or "unknown host"
        summary = _summary(ctx, hostname)
        cause = top.summary if top else f"Alert condition matched for {ctx.incident.get('title') or 'incident'}."
        action = _action(ctx, top)
        provider = "builtin-analyst"
        llm_note = None
        llm_payload = self.llm.complete_json(
            "You are a read-only SRE assistant. Never claim you changed infrastructure. "
            "Treat listed facts as facts. Treat hypotheses as hypotheses. "
            "Return ONLY JSON with keys: summary, likely_cause, recommended_action, limitations (array of strings).",
            "Sanitize-safe RCA context follows. Do not invent metrics.\n" + prompt_context(ctx.to_dict()),
        )
        if llm_payload:
            provider = "forgerca-llm"
            summary = str(llm_payload.get("summary") or summary).strip() or summary
            cause = str(llm_payload.get("likely_cause") or cause).strip() or cause
            action = validate_recommendation(str(llm_payload.get("recommended_action") or action))
            extra = llm_payload.get("limitations") or []
            if isinstance(extra, list):
                ctx.limitations.extend(str(item) for item in extra if item)
        elif self.llm.get_name() != "none":
            why = str(getattr(self.llm, "last_error", "") or "LLM unreachable").strip()
            llm_note = f"{why}; used ForgeRCA deterministic analysis on collected evidence."
            ctx.limitations.append(llm_note)

        if ctx.maintenance:
            ctx.limitations.append("Overlapping maintenance reduces confidence that this is unexpected.")
        ctx.limitations.append(
            "Confidence is a simple ForgeSRE score (evidence + anomalies + history), not a validated model."
        )
        action = validate_recommendation(action)
        visual = _visual(ctx, top)
        result = {
            "incident_id": ctx.incident.get("number") or "",
            "status": "completed",
            "root_cause": {"summary": cause, "confidence": confidence, "hypothesis_id": top.id if top else None},
            "facts": facts,
            "hypotheses": [item.to_dict() for item in hypotheses],
            "anomalies": [item.to_dict() for item in ctx.anomalies],
            "supporting_evidence": top.supporting_evidence if top else [],
            "contradicting_evidence": top.contradicting_evidence if top else [],
            "recommended_actions": [
                {"text": action, "executed": False, "kind": "recommended"},
            ],
            "limitations": list(dict.fromkeys(ctx.limitations)),
            "visual": visual,
            "engine": self.get_name(),
            "engine_version": self.get_version(),
            "llm_provider": self.llm.get_name(),
            "model": self.llm.get_model(),
            "disclaimer": DISCLAIMER,
            "generated_at": utc_now(),
        }
        evidence_lines = [fact["text"] for fact in facts]
        if not evidence_lines:
            evidence_lines = ["Alert payload and asset inventory were available; live metrics were limited."]
        out = {
            "summary": summary,
            "likely_cause": cause,
            "confidence": int(round(confidence * 100)),
            "evidence": evidence_lines,
            "recommended_action": action,
            "disclaimer": DISCLAIMER,
            "provider": provider,
            "result": result,
        }
        if llm_note:
            out["provider_note"] = llm_note
        return out


def get_engine(name: str = "forgerca", llm: LLMProvider | None = None) -> RCAEngine:
    del name
    return ForgeRCA(llm=llm)


def _summary(ctx: RCAContext, hostname: str) -> str:
    title = str(ctx.incident.get("title") or "")
    alertname = ""
    if ctx.alerts:
        alertname = str(ctx.alerts[0].get("alertname") or "")
    family = classify_family(ctx, metric_samples(ctx.evidence))
    return summary_for(family, hostname, title, alertname)


def _action(ctx: RCAContext, top) -> str:
    del top
    playbook = ""
    if ctx.playrules:
        playbook = str(ctx.playrules[0].get("playbook") or "")
    family = classify_family(ctx, metric_samples(ctx.evidence))
    return action_for(family, playbook)


def _visual(ctx: RCAContext, top) -> list[dict[str, Any]]:
    evidence_names = []
    for item in ctx.evidence:
        if item.type == "METRIC":
            content = item.content if isinstance(item.content, dict) else {}
            evidence_names.append(str(content.get("name") or item.evidence_id))
        elif item.type == "LOG":
            evidence_names.append("logs")
    evidence_names = list(dict.fromkeys(evidence_names))[:6]
    return [
        {"id": "alert", "title": "ALERT", "detail": (ctx.alerts[0].get("alertname") if ctx.alerts else ctx.incident.get("title")) or ""},
        {"id": "anomaly", "title": "ANOMALY", "detail": ctx.anomalies[0].summary if ctx.anomalies else "No deterministic anomaly."},
        {"id": "evidence", "title": "EVIDENCE", "detail": ", ".join(evidence_names) or "inventory/alert"},
        {"id": "hypothesis", "title": "HYPOTHESIS", "detail": top.summary if top else ""},
        {"id": "rca", "title": "ROOT CAUSE", "detail": top.summary if top else "Insufficient evidence."},
    ]
