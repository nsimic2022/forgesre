"""Syslog: device → UDP/TCP 514 → Alloy → Loki → ForgeSRE, block by block (registered in app.flows).

Whole flow: 514 bound on the host → Alloy up (its own syslog and Loki-write counters) → Loki ready
→ which hosts sent syslog in the last 24 h, against the inventory → ForgeSRE reading lines back. One
device (``target`` = asset id): its lines in the last 24 h by hostname and by sender IP. Read-only;
Alloy and Loki are on 127.0.0.1, 3 s per call. Message text is never stored by the check.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.flows import Flow, register, step
from app.settings import settings

TIMEOUT = 3.0
ALLOY_URL = "http://127.0.0.1:12345"


def loki_url() -> str:
    return settings.loki_url.rstrip("/")


def alloy_url() -> str:
    return ALLOY_URL


def _get(url: str, params: dict[str, Any] | None = None) -> tuple[httpx.Response | None, int, str]:
    started = time.monotonic()
    try:
        r = httpx.get(url, params=params, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return None, int((time.monotonic() - started) * 1000), f"{exc.__class__.__name__}"
    ms = int((time.monotonic() - started) * 1000)
    return (r, ms, "") if r.status_code < 300 else (None, ms, f"HTTP {r.status_code}")


def alloy_counters(text: str) -> dict[str, float]:
    """Sums of Alloy's syslog and Loki-write counters from its /metrics text (names vary by version)."""
    wanted = {"loki_source_syslog_entries_total": "received", "loki_source_syslog_parsing_errors_total": "parse_errors",
              "loki_write_sent_entries_total": "sent", "loki_write_dropped_entries_total": "dropped"}
    out = {v: 0.0 for v in wanted.values()}
    for line in text.splitlines():
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name in wanted:
            try:
                out[wanted[name]] += float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
    return out


def _hosts(label: str) -> tuple[list[str], int, str]:
    """Values of one syslog label (hostname or ip) seen in the last 24 h."""
    r, ms, err = _get(f"{loki_url()}/loki/api/v1/label/{label}/values", {"query": '{job="syslog"}', "since": "24h"})
    if r is None:
        return [], ms, err
    try:
        return sorted(str(v) for v in (r.json().get("data") or [])), ms, ""
    except ValueError:
        return [], ms, "unexpected answer"


def _count(selector: str) -> tuple[int | None, int, str]:
    """Lines matching ``selector`` in the last 24 h (Loki instant query)."""
    r, ms, err = _get(f"{loki_url()}/loki/api/v1/query", {"query": f"sum(count_over_time({selector}[24h]))"})
    if r is None:
        return None, ms, err
    try:
        result = (r.json().get("data") or {}).get("result") or []
        return (int(float(result[0]["value"][1])) if result else 0), ms, ""
    except (ValueError, KeyError, IndexError, TypeError):
        return None, ms, "unexpected answer"


def _front_steps() -> tuple[list[dict[str, Any]], bool]:
    """514 bound → Alloy → Loki ready. Returns the steps and whether Loki can be asked."""
    from app.api import syslog_listen_ports

    ports = syslog_listen_ports()
    missing = [p for p, on in (("UDP/514", ports["udp"]), ("TCP/514", ports["tcp"])) if not on]
    steps = [step("listen", "Port 514", "UDP and TCP", "ok" if not missing else ("warn" if len(missing) == 1 else "bad"),
                  "Syslog is listening on UDP/514 and TCP/514." if not missing else f"Not listening on {' and '.join(missing)}.",
                  fix="" if not missing else "docker compose up -d --force-recreate --no-deps alloy")]
    r, ms, err = _get(f"{alloy_url()}/metrics")
    if r is None:
        steps.append(step("alloy", "Alloy", "127.0.0.1:12345", "bad", f"Alloy does not answer: {err}.", ms=ms, fix="docker compose up -d alloy"))
    else:
        c = alloy_counters(r.text)
        bad = c["dropped"] > 0 or c["parse_errors"] > 0
        steps.append(step("alloy", "Alloy", f"{int(c['received'])} received", "warn" if bad else "ok",
                          f"Since Alloy started: {int(c['received'])} syslog lines received, {int(c['parse_errors'])} could not be parsed, "
                          f"{int(c['sent'])} written to Loki, {int(c['dropped'])} dropped.", ms=ms,
                          fix="Parse errors: the device sends RFC5424; set it to BSD (RFC3164) format. Dropped: check Loki." if bad else ""))
    r, ms, err = _get(f"{loki_url()}/ready")
    ok = r is not None
    steps.append(step("loki", "Loki", "/ready", "ok" if ok else "bad", "Loki is ready." if ok else f"Loki is not ready: {err}.", ms=ms,
                      fix="" if ok else "docker compose logs loki && docker compose up -d loki"))
    return steps, ok


def check(db: Session) -> dict[str, Any]:
    """Whole syslog path, and which inventory devices sent nothing in 24 h."""
    from app.models import Asset

    steps, ok = _front_steps()
    if ok:
        hosts, ms, err = _hosts("hostname")
        ips, _, _ = _hosts("ip")
        if err:
            steps.append(step("senders", "Senders", "24 h", "bad", f"Loki did not list senders: {err}.", ms=ms))
        else:
            assets = db.query(Asset).all()
            known = {a.hostname.lower() for a in assets} | {(a.ip or "").strip() for a in assets}
            strangers = [h for h in hosts if h.lower() not in known and h != "unknown"]
            rows = [{"at": "", "status": "in inventory" if h.lower() in known else "not in inventory", "error": "", "subject": h}
                    for h in hosts[:30]]
            steps.append(step("senders", "Senders", f"{len(hosts)} host(s)", "ok" if hosts and not strangers else ("warn" if hosts else "unknown"),
                              (f"{len(hosts)} host(s) and {len(ips)} sender IP(s) sent syslog in 24 h"
                               + (f"; {len(strangers)} not in the inventory, so their lines are not matched to an asset." if strangers else ".")) if hosts
                              else "No device sent syslog in the last 24 h.",
                              ms=ms, rows=rows,
                              fix=("Add those hosts to the inventory (or fix the hostname the device sends)." if strangers else
                                   "" if hosts else "On the device: logging host <this server> (UDP 514, BSD format).")))
        n, ms, err = _count('{job="syslog"}')
        steps.append(step("read", "ForgeSRE", "reads lines", "bad" if err else "ok",
                          f"ForgeSRE cannot query Loki: {err}." if err else f"ForgeSRE reads {n} syslog line(s) of the last 24 h (incident evidence uses them).",
                          ms=ms))
    return {"lanes": [{"id": "in", "title": "Device → 514 → Alloy → Loki → ForgeSRE", "steps": steps}]}


def target_label(db: Session, target: str) -> str:
    from app.flow_snmp import target_label as label

    return label(db, target)


def check_device(db: Session, target: str) -> dict[str, Any]:
    """One device: the shared path, then its lines in 24 h by hostname and by sender IP."""
    from app.flow_snmp import _asset

    asset = _asset(db, target)
    steps, ok = _front_steps()
    if asset is None:
        steps.append(step("asset", "Asset", target, "bad", "No such asset."))
    elif ok:
        for sid, label, value in (("by_host", "By hostname", asset.hostname), ("by_ip", "By sender IP", (asset.ip or "").strip())):
            if not value:
                steps.append(step(sid, label, "—", "unknown", f"The asset has no {label.split()[-1].lower()}."))
                continue
            n, ms, err = _count('{job="syslog",%s="%s"}' % ("hostname" if sid == "by_host" else "ip", value.replace('"', "")))
            steps.append(step(sid, label, value, "bad" if err else ("ok" if n else "warn"),
                              f"Loki did not answer: {err}." if err else (f"{n} line(s) in 24 h." if n else "No line in 24 h."), ms=ms,
                              fix="" if n or err else ("The device sends another hostname: compare with Senders on the whole flow." if sid == "by_host"
                                                        else "The device does not send to this server, or a NAT changes its address.")))
    title = f"{asset.hostname} → 514 → Alloy → Loki" if asset is not None else "Device → 514 → Alloy → Loki"
    return {"lanes": [{"id": "in", "title": title, "steps": steps}]}


register(Flow(
    name="syslog",
    label="Syslog",
    check=check,
    configured=lambda: bool(settings.loki_enabled),
    log_words=("syslog", "loki"),
    services=("core", "alloy", "loki"),
    about="How a device's syslog reaches ForgeSRE: port 514, Alloy, Loki and ForgeSRE reading it back. Open one device from its asset page.",
    target_check=check_device,
    target_label=target_label,
))
