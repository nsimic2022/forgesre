"""Deterministic analysis. The LLM does not invent facts."""

from __future__ import annotations

from typing import Any

from rca.catalog import classify_family, hypotheses_for
from rca.types import Anomaly, EvidenceItem, Hypothesis, RCAContext

THRESHOLDS = {
    "cpu_percent": 80.0,
    "cpu_usage": 80.0,
    "disk_percent": 80.0,
    "filesystem_usage": 80.0,
    "disk_volume_percent": 80.0,
    "memory_percent": 90.0,
    "memory_usage": 90.0,
}

DOWN_WHEN_ZERO = {"up"}


def metric_samples(evidence: list[EvidenceItem]) -> dict[str, tuple[float, EvidenceItem]]:
    found: dict[str, tuple[float, EvidenceItem]] = {}
    for item in evidence:
        if item.type != "METRIC":
            continue
        content = item.content if isinstance(item.content, dict) else {}
        if content.get("error") or item.metadata.get("unavailable"):
            continue
        name = str(content.get("name") or "")
        try:
            value = float(content.get("value"))
        except (TypeError, ValueError):
            continue
        if name:
            found[name] = (value, item)
    return found


def detect_anomalies(context: RCAContext) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    samples = metric_samples(context.evidence)
    for name, (value, item) in samples.items():
        if name in DOWN_WHEN_ZERO and value == 0:
            anomalies.append(
                Anomaly(
                    kind="reachability_failure",
                    summary=f"{name} is 0 (target not reachable from Prometheus).",
                    metric=name,
                    evidence_ids=[item.evidence_id],
                    strength=0.7,
                )
            )
            continue
        threshold = THRESHOLDS.get(name)
        if threshold is not None and value > threshold:
            anomalies.append(
                Anomaly(
                    kind="threshold_violation",
                    summary=f"{name} is {value:.1f}, above threshold {threshold:.0f}.",
                    metric=name,
                    evidence_ids=[item.evidence_id],
                    strength=min(1.0, (value - threshold) / 20.0 + 0.5),
                )
            )
        previous = None
        content = item.content if isinstance(item.content, dict) else {}
        raw_previous = content.get("previous")
        if raw_previous not in (None, ""):
            try:
                previous = float(raw_previous)
            except (TypeError, ValueError):
                previous = None
        if previous is not None and value > previous + 20:
            anomalies.append(
                Anomaly(
                    kind="sudden_increase",
                    summary=f"{name} increased from {previous:.1f} to {value:.1f}.",
                    metric=name,
                    evidence_ids=[item.evidence_id],
                    strength=min(1.0, (value - previous) / 40.0),
                )
            )
    for item in context.evidence:
        content = item.content if isinstance(item.content, dict) else {}
        if item.type == "METRIC" and (content.get("error") or item.metadata.get("unavailable")):
            anomalies.append(
                Anomaly(
                    kind="missing_data",
                    summary="Metrics unavailable.",
                    metric="prometheus",
                    evidence_ids=[item.evidence_id],
                    strength=0.3,
                )
            )
        if item.type == "LOG" and item.metadata.get("unavailable"):
            anomalies.append(
                Anomaly(
                    kind="missing_data",
                    summary="Logs unavailable.",
                    metric="loki",
                    evidence_ids=[item.evidence_id],
                    strength=0.3,
                )
            )
    names = set(samples)
    if "cpu_percent" in names and "disk_percent" in names:
        cpu, cpu_item = samples["cpu_percent"]
        disk, disk_item = samples["disk_percent"]
        if cpu > 80 and disk < 70:
            anomalies.append(
                Anomaly(
                    kind="correlated_metric_change",
                    summary="CPU is elevated while filesystem usage is not.",
                    metric="cpu_percent",
                    evidence_ids=[cpu_item.evidence_id, disk_item.evidence_id],
                    strength=0.45,
                )
            )
    return anomalies


def facts_from(context: RCAContext) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    incident = context.incident
    if incident.get("title") or incident.get("number"):
        facts.append(
            {
                "id": "fact-incident",
                "text": (
                    f"Incident {incident.get('number') or ''} "
                    f"{incident.get('title') or ''} on "
                    f"{context.asset.get('hostname') or incident.get('asset') or 'unknown host'}."
                ).strip(),
                "kind": "observed",
            }
        )
    for alert in context.alerts:
        name = alert.get("alertname") or alert.get("name")
        if name:
            facts.append({"id": f"fact-alert-{name}", "text": f"Alert {name} is firing.", "kind": "observed"})
    for name, (value, item) in metric_samples(context.evidence).items():
        unit = "%" if "percent" in name or name.endswith("_usage") else ""
        facts.append(
            {
                "id": f"fact-{item.evidence_id}",
                "text": f"{name} is {value:.1f}{unit}.",
                "kind": "observed",
                "evidence_ids": [item.evidence_id],
            }
        )
    for anomaly in context.anomalies:
        facts.append(
            {
                "id": f"fact-anomaly-{anomaly.kind}-{anomaly.metric}",
                "text": anomaly.summary,
                "kind": "anomaly",
                "evidence_ids": anomaly.evidence_ids,
            }
        )
    if context.historical_incidents:
        facts.append(
            {
                "id": "fact-history",
                "text": f"{len(context.historical_incidents)} previous related incident(s) on this asset.",
                "kind": "observed",
            }
        )
    if context.maintenance:
        facts.append(
            {
                "id": "fact-maintenance",
                "text": "Asset has an overlapping maintenance window.",
                "kind": "observed",
            }
        )
    if context.playrules:
        rule = context.playrules[0]
        book = rule.get("playbook") or "n/a"
        facts.append(
            {
                "id": "fact-playrule",
                "text": f"Playrule {rule.get('name') or 'matched'} maps to playbook {book}. Not executed.",
                "kind": "observed",
            }
        )
    family = classify_family(context, metric_samples(context.evidence))
    if family in {"linux-cpu", "linux-memory", "linux-disk", "linux-host", "windows"}:
        facts.append(
            {
                "id": "fact-no-process-list",
                "text": "No process list was collected. ForgeSRE is read-only and does not run top or ps on the host.",
                "kind": "limitation",
            }
        )
    if family in {"switch", "router", "firewall", "snmp"} and not _has_interface_metric(context.evidence):
        facts.append(
            {
                "id": "fact-no-ifoper-list",
                "text": "No per-interface SNMP ifOperStatus list was used in this investigation. snmp_exporter may still walk if_mib for alerts and the asset page.",
                "kind": "limitation",
            }
        )
    return facts


def _has_interface_metric(evidence: list[EvidenceItem]) -> bool:
    for item in evidence:
        if item.type != "METRIC":
            continue
        content = item.content if isinstance(item.content, dict) else {}
        name = str(content.get("name") or "").lower()
        if name in {"ifoperstatus", "ifadminstatus"} or "ifoper" in name:
            return True
    return False


def candidate_causes(context: RCAContext) -> list[Hypothesis]:
    text = _corpus(context)
    samples = metric_samples(context.evidence)
    family = classify_family(context, samples)
    catalog = hypotheses_for(family)
    disk_metrics = {"disk_percent", "disk_volume_percent", "filesystem_usage"}
    cpu_metrics = {"cpu_percent", "cpu_usage"}
    mem_metrics = {"memory_percent", "memory_usage"}

    hypotheses: list[Hypothesis] = []
    for hid, summary, keywords in catalog:
        supporting: list[str] = []
        contradicting: list[str] = []
        for item in context.evidence:
            blob = str(item.content).lower()
            if any(key in blob for key in keywords):
                supporting.append(item.evidence_id)
            if item.type == "METRIC":
                content = item.content if isinstance(item.content, dict) else {}
                name = str(content.get("name") or "")
                try:
                    value = float(content.get("value"))
                except (TypeError, ValueError):
                    continue
                if family == "linux-disk" and name in disk_metrics and value < 50:
                    contradicting.append(item.evidence_id)
                if family == "linux-cpu" and name in cpu_metrics and value < 40 and hid == "high-process":
                    contradicting.append(item.evidence_id)
                if family == "linux-memory" and name in mem_metrics and value < 50:
                    contradicting.append(item.evidence_id)
                if family in {"snmp", "linux-host"} and name == "up" and value == 1:
                    contradicting.append(item.evidence_id)
        for anomaly in context.anomalies:
            metric = anomaly.metric or ""
            if family == "linux-disk" and "disk" in metric:
                supporting.extend(anomaly.evidence_ids)
            if family == "linux-cpu" and "cpu" in metric:
                supporting.extend(anomaly.evidence_ids)
            if family == "linux-memory" and "memory" in metric:
                supporting.extend(anomaly.evidence_ids)
            if family in {"snmp", "linux-host", "switch", "router", "firewall"} and metric == "up":
                supporting.extend(anomaly.evidence_ids)
        supporting = list(dict.fromkeys(supporting))
        contradicting = list(dict.fromkeys(eid for eid in contradicting if eid not in supporting))
        score = 0.25 + 0.1 * min(4, len(supporting)) - 0.1 * len(contradicting)
        if any(key in text for key in keywords):
            score += 0.12
        hypotheses.append(
            Hypothesis(
                id=hid,
                summary=summary,
                supporting_evidence=supporting,
                contradicting_evidence=contradicting,
                confidence=round(min(0.93, max(0.12, score)), 2),
            )
        )
    hypotheses.sort(key=lambda item: item.confidence, reverse=True)
    for index, item in enumerate(hypotheses, start=1):
        item.rank = index
    return hypotheses


def score_confidence(
    *,
    anomalies: list[Anomaly],
    hypotheses: list[Hypothesis],
    history: list,
    maintenance: list,
    sources_ok: bool,
) -> float:
    score = 0.0
    score += min(0.45, 0.15 * len(anomalies))
    supporting = len(hypotheses[0].supporting_evidence) if hypotheses else 0
    contradicting = len(hypotheses[0].contradicting_evidence) if hypotheses else 0
    score += min(0.40, 0.10 * supporting)
    score -= min(0.30, 0.10 * contradicting)
    if history:
        score += 0.10
    if maintenance:
        score -= 0.20
    if not sources_ok:
        score -= 0.15
    return round(min(0.95, max(0.10, score)), 2)


def _corpus(context: RCAContext) -> str:
    parts = [str(context.incident), str(context.alerts)]
    for item in context.evidence:
        parts.append(str(item.content))
    return " ".join(parts).lower()
