"""Deterministic analysis. The LLM does not invent facts."""

from __future__ import annotations

from typing import Any

from rca.types import Anomaly, EvidenceItem, Hypothesis, RCAContext

BASELINES = {
    "cpu_percent": (20.0, 40.0),
    "cpu_usage": (20.0, 40.0),
    "disk_percent": (30.0, 70.0),
    "filesystem_usage": (30.0, 70.0),
    "disk_volume_percent": (30.0, 70.0),
}

THRESHOLDS = {
    "cpu_percent": 80.0,
    "cpu_usage": 80.0,
    "disk_percent": 80.0,
    "filesystem_usage": 80.0,
    "disk_volume_percent": 80.0,
}


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
        low, high = BASELINES.get(name, (None, None))
        if high is not None and value > high + 20:
            anomalies.append(
                Anomaly(
                    kind="sudden_increase",
                    summary=f"{name} increased from a typical {low:.0f}–{high:.0f} range to {value:.1f}.",
                    metric=name,
                    evidence_ids=[item.evidence_id],
                    strength=min(1.0, (value - high) / 40.0),
                )
            )
        if low is not None and value < max(0.0, low - 15) and name.endswith("percent"):
            anomalies.append(
                Anomaly(
                    kind="sudden_decrease",
                    summary=f"{name} dropped to {value:.1f}, below typical {low:.0f}–{high:.0f}.",
                    metric=name,
                    evidence_ids=[item.evidence_id],
                    strength=0.4,
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
    return facts


def candidate_causes(context: RCAContext) -> list[Hypothesis]:
    text = _corpus(context)
    alertname = " ".join(str((alert or {}).get("alertname") or "") for alert in context.alerts).lower()
    title = str(context.incident.get("title") or "").lower()
    samples = metric_samples(context.evidence)
    cpu = samples.get("cpu_percent", (None, None))[0]
    disk = samples.get("disk_percent", samples.get("disk_volume_percent", (None, None)))[0]
    disk_alert = any(word in alertname or word in title for word in ("disk", "file", "filesystem"))
    cpu_alert = any(word in alertname or word in title for word in ("cpu", "load"))
    diskish = disk_alert or (disk is not None and disk > 80 and not cpu_alert)
    cpuish = cpu_alert or (cpu is not None and cpu > 80 and not disk_alert)

    if diskish:
        catalog = [
            ("log-growth", "Rapid log growth may be consuming disk space.", ("log", "journal", "syslog", "grow")),
            ("database-growth", "Database growth may be consuming disk space.", ("postgres", "mysql", "database", "wal")),
            ("temp-files", "Temporary files may be consuming disk space.", ("tmp", "temp", "cache")),
            ("backup-files", "Backup files may be consuming disk space.", ("backup", "dump", "snapshot")),
            ("app-data", "Application data growth may be consuming disk space.", ("data", "upload", "volume")),
            ("process-activity", "Unexpected process activity may be writing data quickly.", ("write", "i/o", "process")),
        ]
    else:
        catalog = [
            ("high-process", "High process activity is consuming CPU.", ("cpu", "process", "load")),
            ("runaway-job", "A runaway job or cron task may be burning CPU.", ("cron", "job", "batch")),
            ("noisy-neighbor", "Another tenant or container may be competing for CPU.", ("noisy", "neighbor", "cgroup")),
            ("missing-idle", "CPU stayed elevated rather than returning to baseline.", ("sustained", "elevated")),
        ]

    hypotheses: list[Hypothesis] = []
    for hid, summary, keywords in catalog:
        supporting: list[str] = []
        contradicting: list[str] = []
        for item in context.evidence:
            blob = str(item.content).lower()
            if any(key in blob for key in keywords) or item.type in {"ALERT", "METRIC"} and hid.split("-")[0] in blob:
                supporting.append(item.evidence_id)
            if item.type == "METRIC":
                content = item.content if isinstance(item.content, dict) else {}
                name = str(content.get("name") or "")
                try:
                    value = float(content.get("value"))
                except (TypeError, ValueError):
                    continue
                if diskish and name in {"disk_percent", "disk_volume_percent", "filesystem_usage"} and value < 50:
                    contradicting.append(item.evidence_id)
                if cpuish and name in {"cpu_percent", "cpu_usage"} and value < 40 and hid == "high-process":
                    contradicting.append(item.evidence_id)
        for anomaly in context.anomalies:
            if (diskish and "disk" in anomaly.metric) or (cpuish and "cpu" in anomaly.metric):
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
    score = 0.45
    score += min(0.20, 0.08 * len(anomalies))
    supporting = len(hypotheses[0].supporting_evidence) if hypotheses else 0
    contradicting = len(hypotheses[0].contradicting_evidence) if hypotheses else 0
    score += min(0.20, 0.05 * supporting)
    score -= min(0.15, 0.05 * contradicting)
    if history:
        score += 0.08
    if maintenance:
        score -= 0.20
    if not sources_ok:
        score -= 0.15
    return round(min(0.95, max(0.15, score)), 2)


def _corpus(context: RCAContext) -> str:
    parts = [str(context.incident), str(context.alerts)]
    for item in context.evidence:
        parts.append(str(item.content))
    return " ".join(parts).lower()
