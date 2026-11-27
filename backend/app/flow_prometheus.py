"""Prometheus → Alertmanager → ForgeSRE: the main alarm path, block by block (registered in app.flows).

In: Prometheus asks ForgeSRE for its targets (HTTP SD) → targets up → alert rules (loaded, healthy,
firing) → Alertmanager (ready, silences, suppressed alerts) → what Alertmanager sent to the webhook
(its own counters) → network → ForgeSRE webhook → incident. Out: ForgeSRE reading Prometheus and
Alertmanager. All read-only on 127.0.0.1, 3 s per call. No receiver config (it can hold tokens) is
ever read back.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.flows import Flow, previous_result, received_steps, register, step
from app.settings import settings

TIMEOUT = 3.0


def prom_url() -> str:
    return settings.prometheus_url


def am_url() -> str:
    return settings.alertmanager_url


def _get(base: str, path: str, params: dict[str, Any] | None = None) -> tuple[Any, int, str]:
    """GET base+path; (json or text, ms, error)."""
    started = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{base.rstrip('/')}{path}", params=params)
        ms = int((time.monotonic() - started) * 1000)
        if r.status_code >= 400:
            return None, ms, f"HTTP {r.status_code}"
        ctype = r.headers.get("content-type", "")
        return (r.json() if "json" in ctype else r.text), ms, ""
    except (httpx.HTTPError, ValueError) as exc:
        return None, int((time.monotonic() - started) * 1000), f"{exc.__class__.__name__}: {exc}"[:200]


def _prom_query(expr: str) -> tuple[float | None, str]:
    data, _, err = _get(prom_url(), "/api/v1/query", {"query": expr})
    if err or not isinstance(data, dict):
        return None, err or "no answer"
    result = (data.get("data") or {}).get("result") or []
    if not result:
        return 0.0, ""
    try:
        return float(result[0]["value"][1]), ""
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "unexpected answer"


def counters(text: str) -> dict[str, float]:
    """Alertmanager's webhook counters from its /metrics text: sent and failed (all reasons)."""
    sent = failed = 0.0
    for line in text.splitlines():
        if not line.startswith("alertmanager_notifications") or 'integration="webhook"' not in line:
            continue
        try:
            value = float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
        if line.startswith("alertmanager_notifications_failed_total"):
            failed += value
        elif line.startswith("alertmanager_notifications_total"):
            sent += value
    return {"sent": sent, "failed": failed}


def _in_lane(db: Session) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    prom, am = prom_url(), am_url()
    # 1. HTTP SD: Prometheus reads its target list from ForgeSRE.
    failures, err = _prom_query("sum(increase(prometheus_sd_http_failures_total[15m]))")
    if failures is None:
        steps.append(step("sd", "Target list", "HTTP SD", "unknown", f"Prometheus does not answer: {err}.",
                          fix="./forgesre logs prometheus; docker compose ps prometheus"))
    else:
        steps.append(step("sd", "Target list", "HTTP SD from ForgeSRE", "ok" if failures < 1 else "warn",
                          "Prometheus reads its targets from ForgeSRE without errors." if failures < 1
                          else f"{failures:.0f} failed target refresh(es) in 15 min (ForgeSRE restarting or the SD token).",
                          fix="" if failures < 1 else "Normal right after ./forgesre update. If it stays: ALERTMANAGER_WEBHOOK_TOKEN in Prometheus's http_sd config and ./forgesre doctor."))
    # 2. Targets.
    data, ms, err = _get(prom, "/api/v1/targets", {"state": "active"})
    targets = ((data or {}).get("data") or {}).get("activeTargets") or [] if isinstance(data, dict) else []
    if err:
        steps.append(step("targets", "Targets", "scrape", "bad", f"Prometheus does not answer: {err}.", ms=ms,
                          fix="docker compose ps prometheus; ./forgesre logs prometheus"))
    else:
        down = [t for t in targets if t.get("health") != "up"]
        rows = [{"at": "", "status": t.get("health", ""), "error": str(t.get("lastError") or "")[:200],
                 "subject": f"{t.get('scrapePool', '')} {(t.get('labels') or {}).get('instance', '')}"} for t in down[:20]]
        steps.append(step("targets", "Targets", f"{len(targets)} scraped", "ok" if not down else ("warn" if len(down) < len(targets) else "bad"),
                          f"{len(targets) - len(down)} of {len(targets)} up." + (f" Down: {', '.join(r['subject'].strip() for r in rows[:4])}." if down else ""),
                          ms=ms, rows=rows, fix="" if not down else "A down target fires its own alarm (TargetDown); open it in Prometheus › Status › Targets for the error."))
    # 3. Rules.
    data, ms, err = _get(prom, "/api/v1/rules", {"type": "alert"})
    if err:
        steps.append(step("rules", "Alert rules", "rules", "unknown", err, ms=ms))
    else:
        rules = [r for g in ((data or {}).get("data") or {}).get("groups") or [] for r in g.get("rules") or []]
        broken = [r for r in rules if r.get("health") not in (None, "ok", "unknown")]
        firing = [r for r in rules if r.get("state") == "firing"]
        rows = [{"at": "", "status": r.get("health", ""), "error": str(r.get("lastError") or "")[:200], "subject": r.get("name", "")} for r in broken[:20]]
        steps.append(step("rules", "Alert rules", f"{len(rules)} loaded", "bad" if broken else ("ok" if rules else "warn"),
                          (f"{len(broken)} rule(s) fail to evaluate: {', '.join(r['subject'] for r in rows[:4])}." if broken
                           else f"{len(rules)} rules evaluate; {len(firing)} firing now." if rules else "No alert rules are loaded."),
                          ms=ms, rows=rows, fix="Fix the expression in monitoring/alerts.yml (docs/alerting.md), then ./forgesre update." if broken
                          else ("" if rules else "./forgesre update renders monitoring/alerts.yml; check ./forgesre logs prometheus.")))
    # 4. Alertmanager.
    _, ms, err = _get(am, "/-/ready")
    if err:
        steps.append(step("alertmanager", "Alertmanager", "ready", "bad", f"Alertmanager does not answer: {err}.", ms=ms,
                          fix="docker compose ps alertmanager; ./forgesre logs alertmanager"))
        steps.append(step("am_sent", "Alertmanager sent", "webhook", "unknown", "Alertmanager does not answer."))
        return steps
    silences, _, _ = _get(am, "/api/v2/silences")
    active_sil = [s for s in (silences or []) if isinstance(s, dict) and (s.get("status") or {}).get("state") == "active"]
    alerts, _, _ = _get(am, "/api/v2/alerts", {"active": "true", "silenced": "true", "inhibited": "true"})
    alerts = alerts if isinstance(alerts, list) else []
    suppressed = [a for a in alerts if (a.get("status") or {}).get("state") == "suppressed"]
    rows = [{"at": str(s.get("endsAt") or "")[:16], "status": "silence", "error": "",
             "subject": f"{', '.join(m.get('name', '') + ('=' if m.get('isEqual', True) else '!=') + str(m.get('value', '')) for m in s.get('matchers') or [])} — {s.get('createdBy', '')}: {s.get('comment', '')}"[:200]}
            for s in active_sil[:20]]
    steps.append(step("alertmanager", "Alertmanager", f"{len(alerts)} alert(s)", "warn" if suppressed else "ok",
                      f"Ready; {len(alerts)} active alert(s), {len(suppressed)} held back by a silence or inhibition; {len(active_sil)} active silence(s)."
                      + (" Held-back alerts never reach ForgeSRE." if suppressed else ""),
                      ms=ms, rows=rows, fix="Check the silences listed (who, until when) in Alertmanager or Grafana › Alerting." if suppressed else ""))
    # 5. What Alertmanager sent to ForgeSRE: its own counters, compared with the last check.
    text, ms, err = _get(am, "/metrics")
    if err or not isinstance(text, str):
        steps.append(step("am_sent", "Alertmanager sent", "webhook", "unknown", f"Cannot read Alertmanager's counters: {err}.", ms=ms))
    else:
        now = counters(text)
        before = previous_result(db, "prometheus") or {}
        old = next((s.get("counters") for lane in before.get("lanes", []) for s in lane.get("steps", []) if s.get("id") == "am_sent"), None) or {}
        new_failed = now["failed"] - float(old.get("failed", now["failed"])) if old and now["failed"] >= float(old.get("failed", 0)) else 0.0
        status = "bad" if new_failed > 0 else "ok"
        s = step("am_sent", "Alertmanager sent", "webhook", status,
                 f"{now['sent']:.0f} webhook sends since Alertmanager started, {now['failed']:.0f} failed"
                 + (f"; {new_failed:.0f} new failure(s) since the last check." if new_failed > 0 else "."),
                 ms=ms, fix="Alertmanager cannot deliver to ForgeSRE: ./forgesre logs alertmanager (look for 'Notify ... failed'); check the webhook URL and token in monitoring/alertmanager.yml, then ./forgesre update." if status == "bad" else "",
                 log={"direction": "in", "kinds": ["webhook"]})
        s["counters"] = now
        steps.append(s)
    steps += received_steps(db, "prometheus", sender="Alertmanager", endpoint="/api/v1/webhooks/alertmanager",
                            token_name="ALERTMANAGER_WEBHOOK_TOKEN", token_set=bool(settings.webhook_token), incident_source="prometheus")
    return steps


def _out_lane() -> list[dict[str, Any]]:
    steps = []
    started = time.monotonic()
    value, err = _prom_query("count(up)")
    ms = int((time.monotonic() - started) * 1000)
    steps.append(step("prom_api", "Prometheus API", re.sub(r"^https?://", "", prom_url()), "bad" if value is None else ("warn" if ms > 2000 else "ok"),
                      f"Answered in {ms} ms; {value:.0f} series of up." if value is not None else f"No answer: {err}.", ms=ms,
                      fix="docker compose ps prometheus; ./forgesre logs prometheus" if value is None else ("Prometheus is slow: check the host's CPU and disk." if ms > 2000 else "")))
    _, ms, err = _get(am_url(), "/api/v2/status")
    steps.append(step("am_api", "Alertmanager API", re.sub(r"^https?://", "", am_url()), "bad" if err else "ok",
                      f"Answered in {ms} ms." if not err else f"No answer: {err}.", ms=ms,
                      fix="docker compose ps alertmanager" if err else ""))
    return steps


def check(db: Session) -> dict[str, Any]:
    return {"lanes": [{"id": "in", "title": "Prometheus → Alertmanager → ForgeSRE (alarms)", "steps": _in_lane(db)},
                      {"id": "out", "title": "ForgeSRE → Prometheus and Alertmanager (read-only)", "steps": _out_lane()}]}


FLOW = register(Flow(
    name="prometheus", label="Prometheus", check=check, configured=lambda: True,
    log_words=("alertmanager", "webhooks/alertmanager", "/api/v1/sd", "http_sd"), services=("core", "prometheus", "alertmanager"),
    about="How an alarm travels from a Prometheus target through Alertmanager to a ForgeSRE incident.",
))
