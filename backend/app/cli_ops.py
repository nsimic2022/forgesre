"""Host CLI helpers: cookie session + incident/history printers."""

from __future__ import annotations

import json
import os
import subprocess
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from app.asset_probe import (
    DEFAULT_TIMEOUT,
    ad_hoc_item,
    asset_selector_hit,
    classification_patch,
    format_report,
    looks_like_host,
    overall_exit,
    probe_target,
    select_assets,
)
from app.asset_verify import (
    format_verify_report,
    urllib_am_health,
    urllib_prom_query,
    urllib_prom_targets,
    verify_exit,
    verify_target,
)
from app.cli_view import color_enabled, format_board, format_detail, format_history_rows
from app.demo_ids import is_lab_inventory_row

ROOT = Path(__file__).resolve().parents[2]
SESSION_PATH = ROOT / "data" / "cli.session"


def _secrets() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / "secrets" / "secrets.env"
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key] = value
    return env


# Upper bound for one curl call; Core answers in well under a second when healthy.
CURL_TIMEOUT = 60


def _curl(args: list[str], *, check: bool = True, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args, check=check, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=CURL_TIMEOUT
    )


def _login(port: str, jar: Path, email: str, password: str) -> None:
    jar.parent.mkdir(parents=True, exist_ok=True)
    # Password goes in on stdin: curl's argv is world-readable in ps.
    result = _curl(
        [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-c",
            str(jar),
            "-b",
            str(jar),
            "-X",
            "POST",
            f"http://127.0.0.1:{port}/login",
            "--data-urlencode",
            f"email={email}",
            "--data-urlencode",
            "password@-",
        ],
        stdin=password.encode(),
    )
    code = result.stdout.decode().strip()
    if code not in {"200", "302"}:
        raise SystemExit(f"login failed HTTP {code} (check email/password)")


def _me(port: str, jar: Path) -> dict[str, Any] | None:
    result = _curl(
        [
            "curl",
            "-sS",
            "-c",
            str(jar),
            "-b",
            str(jar),
            f"http://127.0.0.1:{port}/api/v1/me",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.decode())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("email"):
        return None
    return data


def ensure_jar(port: str) -> tuple[Path, dict[str, Any] | None]:
    """Prefer ./forgesre login session; otherwise install admin from secrets.env."""
    if SESSION_PATH.exists() and SESSION_PATH.stat().st_size > 0:
        me = _me(port, SESSION_PATH)
        if me:
            return SESSION_PATH, me
    secrets = _secrets()
    email = secrets.get("FORGESRE_ADMIN_EMAIL", "")
    password = secrets.get("FORGESRE_ADMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit("No CLI session. Run: ./forgesre login")
    handle = tempfile.NamedTemporaryFile(prefix="forgesre-cli-", suffix=".jar", delete=False)
    handle.close()
    jar = Path(handle.name)
    _login(port, jar, email, password)
    me = _me(port, jar)
    return jar, me


def who_line(me: dict[str, Any] | None) -> str:
    if not me:
        return ""
    return f"{me.get('email', '')} ({me.get('role', '')})"


def get_json(port: str, jar: Path, path: str) -> Any:
    raw = subprocess.check_output(
        ["curl", "-fsS", "--max-time", "30", "-c", str(jar), "-b", str(jar), f"http://127.0.0.1:{port}{path}"],
        timeout=CURL_TIMEOUT,
    )
    return json.loads(raw)


def post_json(port: str, jar: Path, path: str, payload: dict[str, Any]) -> Any:
    raw = subprocess.check_output(
        [
            "curl",
            "-fsS",
            "--max-time",
            "30",
            "-c",
            str(jar),
            "-b",
            str(jar),
            "-H",
            "Content-Type: application/json",
            "-X",
            "POST",
            "-d",
            json.dumps(payload),
            f"http://127.0.0.1:{port}{path}",
        ],
        timeout=CURL_TIMEOUT,
    )
    if not raw:
        return {}
    return json.loads(raw)


def cmd_login(port: str, email: str, password: str) -> None:
    _login(port, SESSION_PATH, email, password)
    me = _me(port, SESSION_PATH)
    if me is None:
        raise SystemExit("login did not establish a session")
    os.chmod(SESSION_PATH, 0o600)
    print(f"logged in as {who_line(me)}")
    print(f"cookie: {SESSION_PATH}")


def cmd_logout() -> None:
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
    print("logged out (CLI will use install admin from secrets.env if readable)")


def cmd_whoami(port: str) -> None:
    jar, me = ensure_jar(port)
    try:
        print(who_line(me) or "not logged in")
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def _incident_number(args: list[str]) -> str:
    for item in args:
        text = item.strip()
        if text.upper().startswith("INC-") and "-" in text:
            return "INC-" + text.split("-", 1)[1]
    return ""


def cmd_incidents(port: str, args: list[str]) -> None:
    number = _incident_number(args)
    jar, me = ensure_jar(port)
    try:
        if number:
            data = get_json(port, jar, f"/api/v1/incidents/{quote(number, safe='.-_:')}")
            sys.stdout.write(format_detail(data))
            return
        rows = get_json(port, jar, "/api/v1/incidents?limit=100")
        if not isinstance(rows, list):
            rows = []
        sys.stdout.write(format_board(rows, who=who_line(me)))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def cmd_history(port: str, args: list[str]) -> None:
    days = "90"
    status = ""
    asset = ""
    number = ""
    i = 0
    while i < len(args):
        item = args[i]
        if item == "--days" and i + 1 < len(args):
            days = args[i + 1]
            i += 2
            continue
        if item == "--status" and i + 1 < len(args):
            status = args[i + 1]
            i += 2
            continue
        if item == "--asset" and i + 1 < len(args):
            asset = args[i + 1]
            i += 2
            continue
        number = item
        i += 1
    found = _incident_number([number]) if number else ""
    if found:
        number = found
    jar, me = ensure_jar(port)
    try:
        if number:
            data = get_json(port, jar, f"/api/v1/incidents/{quote(number, safe='.-_:')}")
            sys.stdout.write(format_detail(data))
            return
        query = {"days": days}
        if status:
            query["status"] = status
        if asset:
            query["asset"] = asset
        data = get_json(port, jar, f"/api/v1/history?{urlencode(query)}")
        rows = data.get("incidents") or []
        sys.stdout.write(
            format_history_rows(
                rows,
                days=int(data.get("days") or days),
                total=data.get("total"),
            )
        )
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def cmd_numbers(port: str) -> None:
    """One incident number per line for bash TAB."""
    jar, _me_user = ensure_jar(port)
    try:
        rows = get_json(port, jar, "/api/v1/incidents?limit=200")
        if not isinstance(rows, list):
            return
        for item in rows:
            number = str(item.get("number") or "").strip()
            if number:
                print(number)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def _is_demo_row(item: dict[str, Any]) -> bool:
    return is_lab_inventory_row(item)


def asset_ref_tokens(rows: list[Any]) -> list[str]:
    """TAB tokens: asset numbers, ids, then hostnames that differ from id."""
    numbered: list[tuple[int, str]] = []
    ids: list[str] = []
    hosts: list[str] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw = item.get("number")
        try:
            numbered.append((int(raw), str(raw)))
        except (TypeError, ValueError):
            pass
        aid = str(item.get("asset_id") or "").strip()
        if aid and aid not in seen:
            ids.append(aid)
            seen.add(aid)
        host = str(item.get("hostname") or "").strip()
        if host and host not in seen:
            hosts.append(host)
            seen.add(host)
    out: list[str] = []
    for _n, text in sorted(numbered, key=lambda pair: pair[0]):
        if text not in out:
            out.append(text)
    out.extend(ids)
    out.extend(hosts)
    return out


def cmd_asset_refs(port: str) -> None:
    """Asset numbers, ids, then hostnames, one per line, for bash TAB."""
    jar, _me_user = ensure_jar(port)
    try:
        rows = get_json(port, jar, "/api/v1/assets")
        if not isinstance(rows, list):
            return
        for token in asset_ref_tokens(rows):
            print(token)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def cmd_assets(port: str, args: list[str]) -> None:
    selector = ""
    for item in args:
        if item in {"-h", "--help"}:
            raise SystemExit("usage: ./forgesre assets [number-or-id-or-hostname-or-ip]")
        if item.startswith("-"):
            raise SystemExit(f"unknown flag: {item}")
        selector = item
    jar, _me = ensure_jar(port)
    try:
        rows = get_json(port, jar, "/api/v1/assets")
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not list assets: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)
    if not isinstance(rows, list):
        rows = []
    if selector:
        rows = [row for row in rows if isinstance(row, dict) and asset_selector_hit(row, selector)]
        if not rows:
            raise SystemExit(f"no asset matching {selector!r}. Try an asset number (#), id, hostname, or IP.")
    print(f"{'#':<5} {'ASSET':<22} {'IP':<16} {'TYPE':<22} {'SCRAPE / SNMP'}")
    print("-" * 84)
    for item in rows:
        scrape = item.get("scrape_address") or ""
        extra = scrape if scrape else ("SNMP UDP/161" if item.get("snmp") else "—")
        number = item.get("number")
        num = str(number) if number is not None else "—"
        print(f"{num:<5} {item.get('asset_id', ''):<22} {item.get('ip', ''):<16} {str(item.get('type', ''))[:22]:<22} {extra}")
    print()
    print(f"{len(rows)} asset(s). # is the stable asset number (gaps stay after Remove). Linux = node_exporter :9100. Windows = windows_exporter :9182. Network device + IP = snmp_exporter.")
    print("Reachability: ./forgesre ping   (ICMP + /metrics; ping alone is not a scrape)")
    print("Live path:    ./forgesre verify (exporter → prometheus → alertmanager → core; not ./forgesre test)")


def format_capacity(asset_id: str, data: dict[str, Any]) -> str:
    """Capacity rows as text: label, note (forecast or history), samples."""
    if data.get("state") == "error":
        return f"{asset_id}: capacity read failed: {data.get('error') or 'unknown error'}\n"
    lines = [f"{asset_id} — last 30 days"]
    for row in data.get("rows") or []:
        days = row.get("forecast_days")
        mark = "!" if days is not None and int(days) <= 30 else " "
        lines.append(f" {mark} {str(row.get('label') or ''):<20} {str(row.get('note') or ''):<28} {row.get('points') or 0} samples")
    if data.get("measured_at"):
        lines.append(f"Read at {data['measured_at']}; cached 15 min (--refresh reads again).")
    return "\n".join(lines) + "\n"


def cmd_capacity(port: str, args: list[str]) -> None:
    """./forgesre capacity ASSET [--refresh]: 30-day disk / interface history and days to full."""
    import time

    usage = "usage: ./forgesre capacity [--refresh] number-or-id-or-hostname-or-ip"
    refresh = "--refresh" in args
    names = [a for a in args if not a.startswith("-")]
    if any(a in {"-h", "--help"} for a in args) or len(names) != 1:
        raise SystemExit(usage)
    jar, _me = ensure_jar(port)
    try:
        rows = get_json(port, jar, "/api/v1/assets")
        hits = [r for r in rows if isinstance(r, dict) and asset_selector_hit(r, names[0])] if isinstance(rows, list) else []
        if not hits:
            raise SystemExit(f"no asset matching {names[0]!r}. Try ./forgesre assets")
        asset_id = str(hits[0].get("asset_id"))
        path = f"/api/v1/assets/{quote(asset_id)}/capacity"
        data = get_json(port, jar, path + ("?refresh=true" if refresh else ""))
        for _ in range(40):  # the jobs worker reads Prometheus / Zabbix; about a minute at most
            if data.get("state") != "pending":
                break
            time.sleep(1.5)
            data = get_json(port, jar, path)
        if data.get("state") == "pending":
            raise SystemExit("still reading; check ./forgesre jobs and try again")
        sys.stdout.write(format_capacity(asset_id, data))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not read capacity: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


SECURITY_MARK = {"ok": "✓", "warn": "!", "bad": "✗", "info": "·", "unknown": "?"}


def format_flow(result: dict[str, Any]) -> str:
    """The Zabbix flow as text: one lane per direction, one row per block, the fix under bad ones."""
    lines = [f"{result.get('label') or 'Zabbix'} flow — checked {str(result.get('checked_at') or '')[:19].replace('T', ' ')} UTC"]
    for lane in result.get("lanes") or []:
        lines.append("")
        lines.append(str(lane.get("title") or lane.get("id")))
        steps = lane.get("steps") or []
        width = max([14] + [len(str(s.get("label") or "")) for s in steps])
        for s in steps:
            ms = f" ({s['ms']} ms)" if s.get("ms") is not None else ""
            lines.append(f"  {SECURITY_MARK.get(str(s.get('status')), '?')} {str(s.get('label') or ''):<{width}}  {s.get('detail') or ''}{ms}")
            for row in (s.get("rows") or [])[:3]:
                lines.append(f"      {str(row.get('at') or '')[:16].replace('T', ' ')}  {row.get('status')}  {row.get('error') or ''}".rstrip())
            if s.get("fix") and s.get("status") in {"bad", "warn", "unknown"}:
                lines.append(f"      Fix: {s['fix']}")
    return "\n".join(lines) + "\n"


def format_exchanges(rows: list[dict[str, Any]]) -> str:
    """Exchange log rows, oldest first, like a tail."""
    out = []
    for r in reversed(rows):
        arrow = "←" if r.get("direction") == "in" else "→"
        code = f" {r['http_status']}" if r.get("http_status") else ""
        out.append(f"{str(r.get('at') or '')[:19].replace('T', ' ')}  {arrow} {str(r.get('kind') or ''):<16} {str(r.get('status') or ''):<8}{code:<5} "
                   f"{str(r.get('ms') or 0):>5} ms  {r.get('peer') or ''}  {r.get('summary') or ''}".rstrip())
    return ("\n".join(out) + "\n") if out else "No exchanges yet.\n"


FLOW_TEST_PATH = {"zabbix": "/api/v1/webhooks/zabbix", "prometheus": "/api/v1/webhooks/alertmanager"}


def cmd_flow(port: str, args: list[str]) -> None:
    """./forgesre flow [NAME[@ASSET] [check|trace [N]|test]]: an integration's flow step by step, its call
    log, or wait for a test call. ``snmp@sw-01`` checks one device. Without NAME: every flow with its
    broken blocks. Read-only; the check runs as a background job in Core."""
    import time as _time

    usage = "usage: ./forgesre flow [NAME[@ASSET] [check | trace [N] | test]]   (./forgesre zabbix = ./forgesre flow zabbix; snmp@sw-01 = one device)"
    if args and args[0] in {"-h", "--help"}:
        raise SystemExit(usage)
    jar, _me = ensure_jar(port)
    try:
        if not args:
            rows = get_json(port, jar, "/api/v1/flows")
            for f in rows if isinstance(rows, list) else []:
                result = f.get("result") or {}
                bad = [s["label"] for lane in result.get("lanes", []) for s in lane["steps"] if s.get("status") == "bad"]
                warn = [s["label"] for lane in result.get("lanes", []) for s in lane["steps"] if s.get("status") == "warn"]
                mark = "✗" if bad else ("!" if warn else ("✓" if result else "?"))
                where = f"broken: {', '.join(bad)}" if bad else (f"warning: {', '.join(warn)}" if warn else ("works" if result else "not checked yet"))
                sys.stdout.write(f"  {mark} {f['name']:<12} {where}\n")
            sys.stdout.write("Details: ./forgesre flow NAME\n")
            return
        name, sub = args[0], (args[1] if len(args) > 1 else "check")
        name, _, target = name.partition("@")
        if sub not in {"check", "flow", "trace", "test"} or (target and sub not in {"check", "flow"}):
            raise SystemExit(usage)
        q = f"?target={quote(target)}" if target else ""
        if sub in {"check", "flow"}:
            started = post_json(port, jar, f"/api/v1/integrations/{name}/flow{q}", {}).get("job")
            data: dict[str, Any] = {}
            for _ in range(40):  # the job does a few network calls with short timeouts
                _time.sleep(1.5)
                data = get_json(port, jar, f"/api/v1/integrations/{name}/flow{q}")
                if data.get("job") == started and data.get("status") in {"done", "failed", "error"}:
                    break
            if not data.get("result"):
                raise SystemExit(f"the flow check did not finish ({data.get('status')}): ./forgesre jobs")
            label = data.get("label", name) + (f" · {data.get('target_label') or target}" if target else "")
            sys.stdout.write(format_flow({**data["result"], "label": label}))
        elif sub == "trace":
            count = int(args[2]) if len(args) > 2 and args[2].isdigit() else 30
            data = get_json(port, jar, f"/api/v1/integrations/{name}/exchanges?limit={min(count, 200)}")
            sys.stdout.write(format_exchanges(data.get("rows") or []))
        else:
            path = FLOW_TEST_PATH.get(name)
            if not path:
                raise SystemExit(f"{name} does not call ForgeSRE; use ./forgesre flow {name}")
            after = int(get_json(port, jar, f"/api/v1/integrations/{name}/exchanges?limit=1").get("last_id") or 0)
            sys.stdout.write(f"Waiting up to 5 minutes for a call. From the sending server run:\n"
                             f"  curl -sS -X POST http://<FORGE-IP>:{port}{path}\n")
            for _ in range(100):
                _time.sleep(3)
                rows = get_json(port, jar, f"/api/v1/integrations/{name}/exchanges?direction=in&after={after}&limit=10").get("rows") or []
                if rows:
                    sys.stdout.write("Received:\n" + format_exchanges(rows))
                    return
            raise SystemExit("Nothing arrived in 5 minutes: the sender cannot reach this port (firewall, URL).")
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not reach Core: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_security(title: str, checks: list[dict[str, Any]]) -> str:
    """Security findings as aligned rows; warn and bad rows carry the fix."""
    lines = [title]
    width = max([22] + [len(str(c.get("label") or "")) for c in checks])
    for c in checks:
        status = str(c.get("status") or "unknown")
        lines.append(f"  {SECURITY_MARK.get(status, '?')} {str(c.get('label') or ''):<{width}}  {c.get('detail') or ''}")
        if c.get("fix") and status in {"warn", "bad", "unknown"}:
            lines.append(f"      Fix: {c['fix']}")
    return "\n".join(lines) + "\n"


def security_summary_line(checks: list[dict[str, Any]], scope: str = "") -> str:
    """One doctor row: ✓ when nothing to do, else how many problems and warnings (``scope``: "on the host")."""
    bad = sum(1 for c in checks if c.get("status") == "bad")
    warn = sum(1 for c in checks if c.get("status") == "warn")
    if not bad and not warn:
        return "✓"
    parts = ([f"{bad} problem(s)"] if bad else []) + ([f"{warn} warning(s)"] if warn else [])
    where = f" {scope}" if scope else ""
    return f"{'✗' if bad else '!'}  {', '.join(parts)}{where}: ./forgesre security"


def cmd_security(port: str, args: list[str]) -> None:
    """./forgesre security [--json] [--host-only] [--summary] [--quiet]: read-only security check.

    Host part runs here (SSH, firewall, ports, file modes, updates) and is saved for Settings ›
    Security; the platform part comes from Core. Exit 1 when a check is "bad" (not with --summary
    or --quiet, which doctor and update use)."""
    from app.host_security import collect, write_report

    flags = set(args)
    unknown = flags - {"--json", "--host-only", "--summary", "--quiet", "-h", "--help"}
    if unknown or flags & {"-h", "--help"}:
        raise SystemExit("usage: ./forgesre security [--json] [--host-only] [--summary] [--quiet]")
    root = Path(__file__).resolve().parents[2]
    host = collect(root)
    write_report(root, host)
    if "--quiet" in flags:
        return
    platform: list[dict[str, Any]] = []
    if "--host-only" not in flags and "--summary" not in flags:
        jar, _me = ensure_jar(port)
        try:
            platform = list(get_json(port, jar, "/api/v1/system/security").get("platform") or [])
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError, AttributeError) as exc:
            platform = [{"key": "core", "label": "Platform checks", "status": "unknown", "detail": f"Core did not answer ({exc.__class__.__name__}).", "fix": "./forgesre doctor"}]
        finally:
            if jar != SESSION_PATH and jar.exists():
                jar.unlink(missing_ok=True)
    every = list(host["checks"]) + platform
    if "--summary" in flags:
        sys.stdout.write(security_summary_line(every, "on the host") + "\n")
        return
    if "--json" in flags:
        sys.stdout.write(json.dumps({"host": host, "platform": platform}, indent=1) + "\n")
    else:
        who = "root" if host.get("as_root") else "a normal user (sudo ./forgesre security also reads the SSH and firewall settings that need root)"
        sys.stdout.write(format_security(f"Host — checked as {who}", host["checks"]))
        if platform:
            sys.stdout.write("\n" + format_security("Platform", platform))
        sys.stdout.write("\nNothing was changed. " + security_summary_line(every).replace(": ./forgesre security", "").replace("✓", "Nothing to do.") + "\n")
    if any(c.get("status") == "bad" for c in every):
        raise SystemExit(1)


METRIC_PERIODS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}


def format_metrics(asset_id: str, data: dict[str, Any], period: str) -> str:
    """Host metric tiles as text, like the card: value now, min / avg / max over the period, alarm lines."""
    lines = [f"{asset_id} — host metrics, last {period}"]
    if data.get("error"):
        lines.append(f"Prometheus: {data['error']}")
    tiles = [t for t in data.get("tiles") or [] if isinstance(t, dict)]
    if not tiles:
        lines.append(data.get("collecting_line") or "No metrics for this asset.")
        return "\n".join(lines) + "\n"
    lines.append(f"  {'METRIC':<22} {'NOW':<14} {'MIN':>8} {'AVG':>8} {'MAX':>8}  ALARM")
    for t in tiles:
        values = [float(v) for v in t.get("series") or [] if isinstance(v, (int, float))]
        stats = [f"{f(values):>8.1f}" for f in (min, lambda v: sum(v) / len(v), max)] if values else [f"{'—':>8}"] * 3
        crit = t.get("crit") if t.get("crit") is not None else t.get("threshold")
        alarm = (f"warning {t['warn']} · " if t.get("warn") is not None else "") + (f"{crit}" if crit is not None else "")
        mark = "!" if t.get("tone") in {"crit", "high"} else " "
        lines.append(f"{mark} {str(t.get('name') or t.get('key'))[:22]:<22} {str(t.get('display') or '—')[:14]:<14} {' '.join(stats)}  {alarm}")
    return "\n".join(lines) + "\n"


def cmd_metrics(port: str, args: list[str]) -> None:
    """./forgesre metrics ASSET [1h|6h|24h|7d]: the host metric card in text (default 24h)."""
    usage = "usage: ./forgesre metrics number-or-id-or-hostname-or-ip [1h|6h|24h|7d]"
    if any(a in {"-h", "--help"} for a in args) or not 1 <= len(args) <= 2:
        raise SystemExit(usage)
    period = args[1] if len(args) == 2 else "24h"
    if period not in METRIC_PERIODS:
        raise SystemExit(usage)
    jar, _me = ensure_jar(port)
    try:
        rows = get_json(port, jar, "/api/v1/assets")
        hits = [r for r in rows if isinstance(r, dict) and asset_selector_hit(r, args[0])] if isinstance(rows, list) else []
        if not hits:
            raise SystemExit(f"no asset matching {args[0]!r}. Try ./forgesre assets")
        asset_id = str(hits[0].get("asset_id"))
        data = get_json(port, jar, f"/api/v1/assets/{quote(asset_id)}/metrics?hours={METRIC_PERIODS[period]}")
        sys.stdout.write(format_metrics(asset_id, data, period))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not read metrics: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_sources(data: dict[str, Any]) -> str:
    """Feeders with last sync and open conflicts, then the conflicts themselves."""
    lines = [f"New devices from sources: {data.get('new_devices', 'add')} (inventory.new_from_sources)", ""]
    lines.append(f"{'SOURCE':<10} {'ON':<4} {'LINKED':>6} {'CONFLICTS':>9}  LAST SYNC")
    for row in data.get("sources") or []:
        last = row.get("last") or {}
        when = f"{str(last.get('at') or '')[:16]} {last.get('status') or ''} {last.get('summary') or ''}".strip() or "never"
        lines.append(f"{row['source']:<10} {'yes' if row.get('enabled') else 'no':<4} {row.get('linked', 0):>6} {row.get('conflicts', 0):>9}  {when}")
    conflicts = data.get("conflicts") or []
    lines.append("")
    lines.append(f"{len(conflicts)} open conflict(s)" + (":" if conflicts else ". Every source agrees with the inventory."))
    for c in conflicts:
        who = c.get("hostname") or c.get("source_id") or "?"
        lines.append(f"  #{c['id']} {c['source']} {who}: {c.get('detail') or (c.get('field', '') + ' ' + c.get('ours', '') + ' -> ' + c.get('theirs', ''))}")
    if conflicts:
        lines.append("Decide in the UI: Inventory > Sources (keep ours or take theirs).")
    return "\n".join(lines) + "\n"


def cmd_sources(port: str) -> None:
    """./forgesre sources: GLPI / Zabbix / NetBox / LibreNMS sync state and open conflicts."""
    jar, _me = ensure_jar(port)
    try:
        sys.stdout.write(format_sources(get_json(port, jar, "/api/v1/inventory/sources")))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not read sources: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_discovery(data: dict[str, Any]) -> str:
    """Discovery state: LibreNMS reachable, mode, running and last read, found devices."""
    lines = [f"Discovery source: LibreNMS ({data.get('librenms_url') or '?'})"]
    lines.append(f"Ready: {'yes' if data.get('ready') else 'no — ' + str(data.get('why') or '')}")
    lines.append(f"Mode: {data.get('mode')} (discovery.mode), enabled: {'yes' if data.get('enabled') else 'no'}")
    scan = data.get("scan")
    if scan:
        lines.append(f"Reading now: job #{scan.get('id')} {scan.get('status')}")
    last = data.get("last")
    if last:
        result = last.get("result") or {}
        line = f"Last read: {str(last.get('at') or '')[:16]} {last.get('status')}"
        if last.get("status") == "error":
            line += f" — {last.get('error')}"
        elif result:
            line += (f" — {result.get('devices', 0)} devices, {result.get('known', 0)} known, "
                     f"{result.get('found', 0)} new, {result.get('added', 0)} added, {result.get('links', 0)} links")
        lines.append(line)
    else:
        lines.append("Last read: never")
    counts = data.get("counts") or {}
    lines.append("Found list: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))
    return "\n".join(lines) + "\n"


def cmd_discovery(port: str, args: list[str]) -> None:
    """./forgesre discovery [read]: LibreNMS discovery state, or queue a read of its devices now."""
    jar, _me = ensure_jar(port)
    try:
        if args and args[0] == "read":
            queued = post_json(port, jar, "/api/v1/discovery/scan", {})
            sys.stdout.write(f"LibreNMS read queued (job #{queued.get('job_id')}, {queued.get('status')}). "
                             "New devices show in Inventory > Add devices > From LibreNMS.\n")
            return
        sys.stdout.write(format_discovery(get_json(port, jar, "/api/v1/discovery/status")))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not read discovery: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def cmd_ping(port: str, args: list[str]) -> None:
    selector = ""
    timeout = DEFAULT_TIMEOUT
    include_demo = False
    i = 0
    while i < len(args):
        item = args[i]
        if item in {"--timeout", "-t"} and i + 1 < len(args):
            try:
                timeout = float(args[i + 1])
            except ValueError:
                raise SystemExit("usage: ./forgesre ping [--timeout seconds] [--demo] [number-or-id-or-hostname-or-ip]")
            if timeout <= 0:
                raise SystemExit("--timeout must be > 0")
            i += 2
            continue
        if item in {"--demo", "--include-demo"}:
            include_demo = True
            i += 1
            continue
        if item in {"-h", "--help"}:
            raise SystemExit("usage: ./forgesre ping [--timeout seconds] [--demo] [number-or-id-or-hostname-or-ip]")
        if item.startswith("-"):
            raise SystemExit(f"unknown flag: {item}")
        selector = item
        i += 1

    jar, _me = ensure_jar(port)
    try:
        rows = get_json(port, jar, "/api/v1/assets")
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not list assets: {exc}") from exc
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)

    if not isinstance(rows, list):
        rows = []
    chosen, skipped_demo = select_assets(
        rows,
        selector,
        include_demo=include_demo,
        is_demo=_is_demo_row,
    )
    if selector and not chosen and looks_like_host(selector):
        chosen = [ad_hoc_item(selector)]
        skipped_demo = 0
    if selector and not chosen:
            raise SystemExit(f"no asset matching {selector!r}. Try an asset number (#), id, hostname, or IP.")
    if not chosen:
        print("No assets with an IP to probe.")
        if skipped_demo:
            print(f"skipped {skipped_demo} demo lab asset(s). Include with: ./forgesre ping --demo")
        print("Add a host under Assets, then rerun ./forgesre ping")
        raise SystemExit(0)

    results = [probe_target(item, timeout=timeout) for item in chosen]
    sys.stdout.write(format_report(results, timeout=timeout, skipped_demo=skipped_demo, color=color_enabled()))
    raise SystemExit(overall_exit(results))



def cmd_verify(port: str, args: list[str]) -> None:
    selector = ""
    timeout = DEFAULT_TIMEOUT
    include_demo = False
    i = 0
    while i < len(args):
        item = args[i]
        if item in {"--timeout", "-t"} and i + 1 < len(args):
            try:
                timeout = float(args[i + 1])
            except ValueError:
                raise SystemExit(
                    "usage: ./forgesre verify [--timeout seconds] [--demo] [number-or-id-or-hostname-or-ip]"
                )
            if timeout <= 0:
                raise SystemExit("--timeout must be > 0")
            i += 2
            continue
        if item in {"--demo", "--include-demo"}:
            include_demo = True
            i += 1
            continue
        if item in {"-h", "--help"}:
            raise SystemExit(
                "usage: ./forgesre verify [--timeout seconds] [--demo] [number-or-id-or-hostname-or-ip]"
            )
        if item.startswith("-"):
            raise SystemExit(f"unknown flag: {item}")
        selector = item
        i += 1

    jar, _me = ensure_jar(port)
    # The same cookie saves live-detected types after probing, so keep a temp jar until the end.
    try:
        _verify_with_jar(port, jar, selector=selector, timeout=timeout, include_demo=include_demo)
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def _verify_with_jar(port: str, jar: Path, *, selector: str, timeout: float, include_demo: bool) -> None:
    try:
        rows = get_json(port, jar, "/api/v1/assets")
        try:
            support = get_json(port, jar, "/api/v1/verify-support")
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
            support = {"assets": {}, "ai_enabled": False, "prometheus_url": "http://127.0.0.1:9090"}
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"could not list assets: {exc}") from exc

    if not isinstance(rows, list):
        rows = []
    chosen, skipped_demo = select_assets(
        rows,
        selector,
        include_demo=include_demo,
        is_demo=_is_demo_row,
    )
    if selector and not chosen:
            raise SystemExit(f"no asset matching {selector!r}. Try an asset number (#), id, hostname, or IP.")
    if not chosen:
        print("No assets to verify.")
        if skipped_demo:
            print(f"skipped {skipped_demo} demo lab asset(s). Include labeled DEMO with: ./forgesre verify --demo")
        print("Add a host under Assets, then rerun ./forgesre verify")
        raise SystemExit(0)

    contexts = support.get("assets") if isinstance(support, dict) else {}
    if not isinstance(contexts, dict):
        contexts = {}
    prom_url = str((support or {}).get("prometheus_url") or "http://127.0.0.1:9090")
    ai_enabled = bool((support or {}).get("ai_enabled"))
    am_health = (support or {}).get("alertmanager") if isinstance(support, dict) else None
    if not isinstance(am_health, dict):
        am_url = str((support or {}).get("alertmanager_url") or "http://127.0.0.1:9093")
        am_health = urllib_am_health(am_url)

    def query_fn(expr: str) -> Any:
        return urllib_prom_query(expr, prom_url)

    fetched: dict[str, Any] = {}

    def targets_fn() -> Any:
        if "targets" not in fetched:
            fetched["targets"] = urllib_prom_targets(prom_url)
        return fetched["targets"]

    results = []
    for item in chosen:
        ctx = contexts.get(str(item.get("asset_id") or "")) or {}
        merged = dict(item)
        extra_asset = ctx.get("asset") if isinstance(ctx, dict) else None
        if isinstance(extra_asset, dict):
            merged.update(extra_asset)
        report = verify_target(
            merged,
            timeout=timeout,
            in_http_sd=bool(ctx.get("in_http_sd")),
            in_snmp_sd=bool(ctx.get("in_snmp_sd")),
            query_fn=query_fn,
            rca=ctx.get("rca") if isinstance(ctx, dict) else None,
            live_metrics=ctx.get("live_metrics") if isinstance(ctx, dict) else None,
            ai_enabled=ai_enabled,
            targets_fn=targets_fn,
            am_health=am_health,
            incident=ctx.get("incident") if isinstance(ctx, dict) else None,
        )
        if report.probe is not None and not report.lab:
            patch = classification_patch(merged, report.probe)
            if patch:
                asset_id = quote(str(report.asset_id or merged.get("asset_id") or ""), safe="")
                try:
                    saved = post_json(port, jar, f"/api/v1/assets/{asset_id}", patch)
                except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
                    saved = {}
                if isinstance(saved, dict):
                    if saved.get("type"):
                        report.type = str(saved.get("type") or report.type)
                    if saved.get("scrape_address") is not None:
                        report.scrape = str(saved.get("scrape_address") or report.scrape)
                    if saved.get("monitoring_profile"):
                        report.profile = str(saved.get("monitoring_profile") or report.profile)
        results.append(report)
    sys.stdout.write(
        format_verify_report(
            results,
            skipped_demo=skipped_demo,
            color=color_enabled(),
            detail=bool(selector),
        )
    )
    raise SystemExit(verify_exit(results))


def put_json(port: str, jar: Path, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    """PUT JSON; returns (HTTP status, parsed body) so callers can show a 400 reason."""
    result = subprocess.run(
        [
            "curl", "-sS", "--max-time", "30", "-c", str(jar), "-b", str(jar),
            "-H", "Content-Type: application/json", "-X", "PUT", "-d", json.dumps(payload),
            "-w", "\n%{http_code}", f"http://127.0.0.1:{port}{path}",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=CURL_TIMEOUT,
    )
    body, _, code = result.stdout.decode("utf-8", "replace").rpartition("\n")
    try:
        parsed = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        parsed = {"detail": body.strip()}
    return int(code or 0), parsed


# Secrets the host CLI may write into secrets/secrets.env. Must match the "secret"
# entries in app/system_settings.py (tests/test_system_settings.py checks it).
SETTABLE_SECRETS = (
    "ALERTMANAGER_WEBHOOK_TOKEN", "GLPI_APP_TOKEN", "GLPI_USER_TOKEN", "ZABBIX_API_TOKEN",
    "LIBRENMS_API_TOKEN", "NETBOX_API_TOKEN", "SMTP_USERNAME", "SMTP_PASSWORD",
    "SLACK_WEBHOOK_URL", "TEAMS_WEBHOOK_URL", "DISCORD_WEBHOOK_URL", "CHANGES_WEBHOOK_TOKEN", "MCP_TOKEN",
    "LDAP_BIND_PASSWORD", "OIDC_CLIENT_SECRET", "CLOUD_LLM_API_KEY", "SMS_GATEWAY_TOKEN", "TICKET_WEBHOOK_TOKEN", "OXIDIZED_TOKEN", "BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY",
    "ZABBIX_WEBHOOK_TOKEN",
)


def write_secret(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a dotenv file, keeping every other line and mode 600."""
    if key not in SETTABLE_SECRETS and not re.fullmatch(r"K8S_TOKEN_[A-Z0-9_]+", key):
        raise SystemExit(f"{key} is not a settable secret. Choose one of: {', '.join(SETTABLE_SECRETS)}")
    value = value.rstrip("\n")
    if any(ch in value for ch in "\n\r") or not value.strip():
        raise SystemExit("value must be one non-empty line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            if not done:
                out.append(f"{key}={value.strip()}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value.strip()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)


def format_settings(data: dict[str, Any]) -> str:
    """Settings grouped like the Settings page; secrets as set / NOT SET, never their value."""
    groups = dict(data.get("groups") or {})
    rows = list(data.get("settings") or [])
    lines: list[str] = []
    for gid, title in groups.items():
        mine = [r for r in rows if r.get("group") == gid]
        if not mine:
            continue
        lines.append(title)
        for r in mine:
            if r.get("store") == "secret":
                shown = "set" if r.get("set") else "NOT SET"
            else:
                value = r.get("effective") if r.get("effective") not in (None, "") else r.get("value")
                shown = ", ".join(map(str, value)) if isinstance(value, list) else ("—" if value in (None, "") else str(value))
            tag = {"secret": " (secret)", "env": " (installer)"}.get(str(r.get("store")), "")
            lines.append(f"  {r.get('key'):<36} {shown}{tag}")
        lines.append("")
    return "\n".join(lines)


def cmd_settings(port: str, args: list[str]) -> None:
    """./forgesre settings [group] | get KEY | set KEY VALUE | test [COMPONENT|all]"""
    jar, _me = ensure_jar(port)
    try:
        sub = args[0] if args else ""
        if sub == "get" and len(args) == 2:
            data = get_json(port, jar, "/api/v1/settings")
            row = next((r for r in data.get("settings", []) if r.get("key") == args[1]), None)
            if row is None:
                raise SystemExit(f"unknown setting {args[1]}")
            sys.stdout.write(json.dumps(row, indent=2) + "\n")
        elif sub == "set" and len(args) >= 3:
            code, body = put_json(port, jar, f"/api/v1/settings/{args[1]}", {"value": " ".join(args[2:])})
            if code != 200:
                raise SystemExit(f"not saved ({code}): {body.get('detail', body)}")
            sys.stdout.write(f"{args[1]} = {body.get('value')}\n")
        elif sub == "test":
            names = args[1:] or ["all"]
            if names == ["all"]:
                data = get_json(port, jar, "/api/v1/settings")
                names = sorted({r["test"] for r in data.get("settings", []) if r.get("test")})
            bad = 0
            for name in names:
                try:
                    result = post_json(port, jar, f"/api/v1/settings/test/{name}", {})
                except subprocess.CalledProcessError:
                    known = sorted({r["test"] for r in get_json(port, jar, "/api/v1/settings").get("settings", []) if r.get("test")})
                    raise SystemExit(f"no test called {name!r}. Tests: {', '.join(known)}") from None
                status = str(result.get("status", "?"))
                why = result.get("why") or result.get("detail") or ""
                bad += status not in {"ok", "disabled", "skipped"}
                sys.stdout.write(f"  {name:<14} {status:<9} {why}\n")
            if bad:
                raise SystemExit(1)
        else:
            data = get_json(port, jar, "/api/v1/settings" + (f"?group={args[0]}" if args else ""))
            sys.stdout.write(format_settings(data))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_jobs(rows: list[dict[str, Any]], counts: dict[str, int] | None = None) -> str:
    """Queue summary plus the newest jobs, one line each; errors keep their message."""
    counts = counts or {}
    if not counts:
        for row in rows:
            counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
    order = ["pending", "running", "error", "done"]
    head = "  ".join(f"{name} {counts.get(name, 0)}" for name in order)
    lines = [f"Jobs  {head}", ""]
    if not rows:
        lines.append("No jobs yet.")
    for row in rows[:20]:
        when = str(row.get("finished_at") or row.get("started_at") or row.get("created_at") or "")[:19].replace("T", " ")
        line = f"#{row.get('id'):<6} {str(row.get('status')):<8} {str(row.get('kind')):<16} {str(row.get('object_id')):<28} {when}"
        if row.get("status") == "error" and row.get("error"):
            line += f"\n         {str(row.get('error')).splitlines()[0][:110]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def cmd_jobs(port: str) -> None:
    jar, _me = ensure_jar(port)
    try:
        data = get_json(port, jar, "/api/v1/jobs?with_counts=1")
        if isinstance(data, dict):
            sys.stdout.write(format_jobs(list(data.get("jobs") or []), dict(data.get("counts") or {})))
        else:
            sys.stdout.write(format_jobs(list(data or [])))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_checks(data: dict) -> str:
    """Certificates, backup age and heartbeats, one line each, failing first."""
    rows = sorted(data.get("results") or [], key=lambda r: (r.get("status") == "ok", r.get("kind"), r.get("target")))
    if not rows:
        return "No check has run yet. Add TLS endpoints with ./forgesre settings set checks.tls_targets host:443, then ./forgesre checks run\n"
    out = [f"{'STATUS':<9} {'CHECK':<10} {'TARGET':<34} DETAIL"]
    for r in rows:
        out.append(f"{str(r.get('status', '')).upper():<9} {r.get('kind', ''):<10} {str(r.get('target', ''))[:34]:<34} {r.get('detail', '')}")
    beats = data.get("heartbeats") or []
    if beats:
        out.append("")
        out.append("Heartbeats: " + ", ".join(f"{b['name']} every {b['every_hours']:g} h" for b in beats))
    return "\n".join(out) + "\n"


def format_pulse(p: dict) -> str:
    """The chat's status line as text: model and connection, last test, ForgeAI queue and its worker."""
    local, test, lane = p.get("local") or {}, p.get("test") or {}, p.get("lane") or {}
    conn = "off" if not local.get("on") else "ok" if local.get("reachable") else f"NOT answering ({local.get('why') or '?'})"
    if test.get("status") == "ok":
        t = f"ok, first token {(test.get('first_ms') or 0) / 1000:.1f} s, total {(test.get('ms') or 0) / 1000:.1f} s"
    elif test.get("status") == "error":
        t = f"FAILED: {test.get('error') or '?'}"
    else:
        t = {"pending": "waiting in the queue", "running": "running", "none": "never run (./forgesre forgeai test)"}.get(str(test.get("status")), "?")
    run = lane.get("running")
    worker = p.get("worker_age_s")
    return (f"Model {local.get('model') or '-'} · connection {conn} · test {t}\n"
            f"Queue: {lane.get('pending', 0)} waiting" + (f", running {run['kind']} for {run['seconds']} s" if run else "")
            + f" · worker {'last seen ' + str(worker) + ' s ago' if worker is not None else 'NOT seen'}"
            + f" · timeout {p.get('timeout_s')} s · thinking {'on' if p.get('thinking') else 'off'}\n")


def format_forgeai(status: dict, calls: list[dict] | None = None) -> str:
    """ForgeAI on/off, model, last week; with an incident, each question and the exact prompts."""
    w = status.get("week") or {}
    model = status.get("loaded_model") or status.get("model") or ""
    reach = "answering" if status.get("reachable") else ("NOT answering: " + str(status.get("why") or "")) if status.get("enabled") else "off"
    out = [
        f"ForgeAI: {'on' if status.get('enabled') else 'off'} · model {model or '-'} · {reach}",
        f"Last 7 days: {w.get('calls', 0)} questions, {w.get('used', 0)} used, {w.get('rejected', 0)} rejected, "
        f"{w.get('errors', 0)} without answer, avg {w.get('avg_ms', 0) / 1000:.1f} s, "
        + (f"rated {w.get('stars_avg')} of 5 stars ({w.get('stars_n')} ratings)" if w.get("stars_n") else "no star ratings yet"),
    ]
    cloud = status.get("cloud") or {}
    if cloud.get("enabled"):
        out.append(f"Cloud model: {cloud.get('provider')} {cloud.get('model')} · "
                   + ("ready (Engineers choose it per question)" if cloud.get("ready") else f"not ready: {cloud.get('why')}"))
    else:
        out.append("Cloud model: off (optional, ai.cloud.enabled)")
    out.append("")
    rows = calls if calls is not None else status.get("last") or []
    for c in rows:
        out.append(f"{str(c.get('at') or '')[:16]}  {str(c.get('status', '')).upper():<8} {c.get('task', ''):<10} {c.get('skill', ''):<18} "
                   f"{(c.get('duration_ms') or 0) / 1000:5.1f} s  {c.get('reason') or ''}")
        if calls is not None and c.get("system_prompt") is not None:
            out += ["  --- system prompt ---", *("  " + line for line in str(c["system_prompt"]).splitlines()),
                    "  --- given to the model ---", "  " + str(c.get("user_prompt") or ""),
                    "  --- model answered ---", "  " + str(c.get("raw_response") or "(nothing)"), ""]
    return "\n".join(out) + "\n"


def format_tuning(r: dict[str, Any]) -> str:
    """One skill: counts, analyst ratings, rejection reasons and the worst-rated answers next to the confirmed cause."""
    ratings = ", ".join(f"{v} {k}" for k, v in (r.get("ratings") or {}).items()) or "no analyst ratings yet"
    out = [f"{r['skill']} (v{r['version']}, last {r['days']} days): {r['calls']} questions, {r['used']} used, "
           f"{r['rejected']} rejected, {r['errors']} no answer; {ratings}"]
    for reason in r.get("reject_reasons") or []:
        out.append(f"  rejected {reason['count']}x: {reason['reason']}")
    for e in (r.get("examples") or [])[:5]:
        out.append(f"  {e['incident']} [{e.get('rating') or 'not rated'}] said: {e['said'][:90]}")
        if e.get("confirmed"):
            out.append(f"  {'':{len(e['incident'])}} confirmed: {e['confirmed'][:90]}")
    return "\n".join(out) + "\n\n"


def format_golden(data: dict) -> str:
    """Golden set: the cases, then the last runs (score, model, skill, language) and the newest per case."""
    cases, runs = data.get("cases") or [], data.get("runs") or []
    if not cases:
        return ("Golden set is empty. Add resolved incidents with a confirmed cause in Settings › ForgeAI skills "
                "› Golden set (Engineer).\n")
    out = [f"{len(cases)} incidents in the golden set" + (" — a run is in progress" if data.get("running") else "")]
    for c in cases:
        out.append(f"  {c['incident'].split('_')[0]:<16} {c['title'][:50]:<50} expects: {', '.join(c['expect'][:4])}")
    if not runs:
        out.append("Never run. Start one with Run golden set in the UI.")
        return "\n".join(out) + "\n"
    out.append(f"{'RUN':<5} {'SCORE':<7} {'MODEL':<28} {'SKILL':<20} LANG  FINISHED")
    for r in runs:
        out.append(f"#{r['id']:<4} {r['passed']}/{r['total']:<5} {str(r['model'])[:28]:<28} {str(r['skill'])[:20]:<20} {r['language']:<5} {r.get('finished_at') or 'running'}")
    for res in runs[0].get("results") or []:
        out.append(f"  {'PASS' if res['passed'] else 'FAIL'} {res['incident']:<16} {res['why'][:40]:<40} {res['ms']} ms")
    for cmp in (data.get("comparisons") or [])[:1]:
        pair = cmp.get("runs") or []
        if len(pair) == 2:
            out.append("Last model comparison:")
            for r in pair:
                out.append(f"  {str(r['model'])[:32]:<32} {r['passed']}/{r['total']:<5} rejected {r.get('rejected', 0)}  avg {r.get('avg_ms', 0) / 1000:.1f} s")
    return "\n".join(out) + "\n"


def cmd_forgeai(port: str, rest: list[str]) -> None:
    """./forgesre forgeai [INC-… | tuning [skill] | docs | test | golden]"""
    import time

    jar, _me = ensure_jar(port)
    try:
        if rest[:1] == ["test"]:
            started = post_json(port, jar, "/api/v1/forgeai/ping", {})
            sys.stdout.write(f"Test queued (job {started.get('job')}); waiting for the model…\n")
            pulse = get_json(port, jar, "/api/v1/forgeai/pulse")
            deadline = time.monotonic() + int(pulse.get("timeout_s") or 90) + 120
            while (pulse.get("test") or {}).get("status") in {"pending", "running"} and time.monotonic() < deadline:
                time.sleep(2)
                pulse = get_json(port, jar, "/api/v1/forgeai/pulse")
            sys.stdout.write(format_pulse(pulse))
            return
        if rest[:1] == ["golden"]:
            sys.stdout.write(format_golden(get_json(port, jar, "/api/v1/forgeai/golden")))
            return
        if rest[:1] == ["docs"]:
            data = get_json(port, jar, "/api/v1/forgeai/docs")
            lines = [f"Document search: {'on' if data.get('embedder') else 'off'} · model {data.get('model')} · "
                     f"{'pgvector' if data.get('pgvector') else 'search in Python (no pgvector)'}"]
            for d in data.get("docs") or []:
                lines.append(f"  #{d['id']:<4} {d['status']:<9} {d['pages']:>4} pages {d['chunks']:>5} chunks  {d['title']}"
                             + (f"  ({d['error']})" if d.get("error") else ""))
            if not data.get("docs"):
                lines.append("  No documents. Upload in Settings › ForgeAI RAG (Engineer).")
            sys.stdout.write("\n".join(lines) + "\n")
            return
        if rest[:1] == ["tuning"]:
            skills = get_json(port, jar, "/api/v1/forgeai/skills").get("skills") or []
            slugs = rest[1:2] or [s["slug"] for s in skills if s.get("task") == "incident"]
            sys.stdout.write("".join(format_tuning(get_json(port, jar, f"/api/v1/forgeai/skills/{quote(s)}/tuning")) for s in slugs))
            return
        status = get_json(port, jar, "/api/v1/forgeai/status")
        calls = get_json(port, jar, f"/api/v1/incidents/{rest[0]}/forgeai")["calls"] if rest else None
        if not rest:
            sys.stdout.write(format_pulse(get_json(port, jar, "/api/v1/forgeai/pulse")))
        sys.stdout.write(format_forgeai(status, calls))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_slos(rows: list[dict]) -> str:
    """One line per SLO: status, availability against target, budget left, 24 h burn."""
    if not rows:
        return "No SLO defined. Add one under Reports › SLOs in the UI.\n"
    out = [f"{'STATUS':<9} {'SLO':<28} {'AVAIL':>9} {'TARGET':>8} {'BUDGET LEFT':>12} {'BURN 24H':>9}"]
    for r in rows:
        out.append(f"{r['status'].upper():<9} {r['name'][:28]:<28} {r['availability']:>8.3f}% {r['target']:>7g}% "
                   f"{r['budget_left_min']:>8.0f} min {r['burn_rate_24h']:>8.1f}x")
    return "\n".join(out) + "\n"


def format_k8s(rows: list[dict]) -> str:
    """One line per cluster."""
    if not rows:
        return "No cluster configured. See docs/kubernetes.md (kubernetes.clusters + K8S_TOKEN_<NAME>).\n"
    out = [f"{'CLUSTER':<16} {'TOKEN':<6} {'NODES':>5} {'WORKLOADS':>9} {'OPEN INC':>8}  VERSION"]
    for r in rows:
        out.append(f"{r['name'][:16]:<16} {'yes' if r['token_set'] else 'NO':<6} {r['nodes']:>5} {r['workloads']:>9} {r['open_incidents']:>8}  {r['version']}")
    return "\n".join(out) + "\n"


def cmd_k8s(port: str, rest: list[str]) -> None:
    """./forgesre kubernetes [sync]"""
    jar, _me = ensure_jar(port)
    try:
        if rest[:1] == ["sync"]:
            post_json(port, jar, "/api/v1/kubernetes/sync", {})
            sys.stdout.write("Cluster sync queued: ./forgesre kubernetes in a minute.\n")
        else:
            sys.stdout.write(format_k8s(list(get_json(port, jar, "/api/v1/kubernetes").get("clusters") or [])))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_alert_quality(data: dict) -> str:
    """Noisiest alerts first: incidents, % acknowledged, % that cleared alone, advice."""
    rows = data.get("rows") or []
    head = f"Last {data.get('days')} days: {data.get('incidents', 0)} incidents"
    if data.get("actionable_pct") is not None:
        head += f", {data['actionable_pct']}% acknowledged by a person"
    if not rows:
        return head + "\nNo incidents in this period.\n"
    out = [head, f"{'ALERT':<32} {'INC':>5} {'ACKED':>6} {'ALONE':>6} {'FLAP':>5}  ADVICE"]
    for r in rows[:40]:
        out.append(f"{r['alertname'][:32]:<32} {r['incidents']:>5} {r['acked_pct']:>5}% {r['self_cleared_pct']:>5}% {r['flapping']:>5}  {r['advice']}")
    return "\n".join(out) + "\n"


def cmd_alert_quality(port: str, rest: list[str]) -> None:
    """./forgesre alert-quality [DAYS]"""
    jar, _me = ensure_jar(port)
    try:
        days = rest[0] if rest and rest[0].isdigit() else "30"
        sys.stdout.write(format_alert_quality(get_json(port, jar, f"/api/v1/reports/alert-quality?days={days}")))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_power(data: dict) -> str:
    """UPS rows, then sensors, then power assets that answer nothing."""
    if data.get("error"):
        return f"Prometheus did not answer: {data['error']}\n"
    ups, sensors, silent = data.get("ups") or [], data.get("sensors") or [], data.get("silent") or []
    if not (ups or sensors or silent):
        return "No UPS, PDU or environment sensor in inventory. Add one with type UPS, PDU or Environment sensor.\n"

    def fmt(value: object, suffix: str = "") -> str:
        return "-" if value is None else f"{value:g}{suffix}"

    out: list[str] = []
    if ups:
        out.append(f"{'UPS':<20} {'STATE':<9} {'SOURCE':<8} {'BATTERY':<9} {'CHARGE':>6} {'MIN':>5} {'LOAD':>5} {'IN V':>5} ALARMS")
        for r in ups:
            out.append(f"{r['asset'][:20]:<20} {r['state']:<9} {r['source']:<8} {r['battery']:<9} {fmt(r['charge'], '%'):>6} "
                       f"{fmt(r['minutes_left']):>5} {fmt(r['load'], '%'):>5} {fmt(r['input_v']):>5} {r['alarms']}")
    if sensors:
        out.append(f"{'SENSOR':<20} {'NAME':<24} {'STATE':<9} VALUE")
        for s in sensors:
            out.append(f"{s['asset'][:20]:<20} {s['name'][:24]:<24} {s['state']:<9} {s['value']:g} {s['unit']}")
    if silent:
        out.append("No SNMP data from: " + ", ".join(silent))
    return "\n".join(out) + "\n"


def cmd_power(port: str) -> None:
    """./forgesre power"""
    jar, _me = ensure_jar(port)
    try:
        sys.stdout.write(format_power(get_json(port, jar, "/api/v1/power")))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_lifecycle(data: dict) -> str:
    """Expiring and expired support, end-of-life and licence dates, soonest first."""
    rows = data.get("rows") or []
    head = f"{data.get('tracked', 0)} dates tracked; warning {data.get('warn_days', 30)} days ahead (support: per asset)"
    if not rows:
        return head + "\nNothing expiring.\n"
    out = [head, f"{'ASSET':<24} {'WHAT':<20} {'DATE':<10} {'DAYS':>5}  STATUS"]
    for r in rows:
        out.append(f"{r['asset'][:24]:<24} {r['label'][:20]:<20} {r['date']:<10} {r['days_left']:>5}  {r['status']}")
    return "\n".join(out) + "\n"


def format_racks(data: dict) -> str:
    """One block per rack: U positions top-down, then devices without a position and overlaps."""
    racks = data.get("racks") or []
    if not racks:
        return "No asset has a rack. Set Location › Rack (and Rack position) on the asset.\n"
    out: list[str] = []
    for r in racks:
        where = " / ".join(x for x in (r.get("site"), r.get("row"), r.get("rack")) if x)
        out.append(f"{where}  ({r['used_u']} of {r['units']} U used)")
        for d in r.get("devices") or []:
            top = d["u"] + d["height"] - 1
            pos = f"U{d['u']}" if d["height"] == 1 else f"U{d['u']}-{top}"
            mark = f"  [{d['incident']}]" if d.get("incident") else ""
            out.append(f"  {pos:<8} {d['hostname'][:32]:<32} {d['type'][:20]}{mark}")
        for d in r.get("unplaced") or []:
            out.append(f"  {'?':<8} {d['hostname'][:32]:<32} no rack position")
        for o in r.get("overlaps") or []:
            out.append(f"  overlap {o}")
    return "\n".join(out) + "\n"


def cmd_lifecycle(port: str, command: str, rest: list[str] | None = None) -> None:
    """./forgesre lifecycle | racks | synthetic | network | known-errors [TEXT]"""
    rest = rest or []
    jar, _me = ensure_jar(port)
    try:
        if command == "racks":
            sys.stdout.write(format_racks(get_json(port, jar, "/api/v1/racks")))
        elif command == "known-errors":
            text = " ".join(rest)
            sys.stdout.write(format_known_errors(get_json(port, jar, f"/api/v1/known-errors?q={quote(text)}")))
        elif command == "network":
            sys.stdout.write(format_network(get_json(port, jar, "/api/v1/network")))
        elif command == "synthetic":
            sys.stdout.write(format_synthetic(get_json(port, jar, "/api/v1/synthetic")))
        else:
            sys.stdout.write(format_lifecycle(get_json(port, jar, "/api/v1/lifecycle")))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def format_synthetic(data: dict) -> str:
    """Synthetic checks: name, kind, target, last probe result and duration."""
    rows = data.get("checks") or []
    if not rows:
        return "No synthetic check. Add one in Settings › Checks › Synthetic checks.\n"
    out = [f"{'CHECK':<24} {'KIND':<5} {'RESULT':<7} {'TIME':>7}  TARGET"]
    for r in rows:
        result = "off" if not r.get("enabled") else "?" if r.get("up") is None else "ok" if r["up"] >= 1 else "FAIL"
        secs = "-" if r.get("seconds") is None else f"{r['seconds']:.2f}s"
        out.append(f"{r['name'][:24]:<24} {r['kind']:<5} {result:<7} {secs:>7}  {r['target']}")
    if data.get("error"):
        out.append(f"Prometheus did not answer: {data['error']}")
    return "\n".join(out) + "\n"


def _bits(bps: float) -> str:
    for unit, size in (("Gb/s", 1e9), ("Mb/s", 1e6), ("kb/s", 1e3)):
        if bps >= size:
            return f"{bps / size:.1f} {unit}"
    return f"{bps:.0f} b/s"


def format_network(data: dict) -> str:
    """BGP peers and OSPF neighbours (problems first), then the busiest interfaces."""
    if data.get("error"):
        return f"Prometheus did not answer: {data['error']}\n"
    out: list[str] = []
    bgp, ospf = data.get("bgp") or [], data.get("ospf") or []
    if bgp:
        out.append(f"{'BGP ON':<20} {'PEER':<16} {'AS':>7}  STATE")
        for r in bgp:
            state = r["state"] + ("" if r["enabled"] else " (admin off)")
            out.append(f"{r['asset'][:20]:<20} {r['peer']:<16} {r['remote_as']:>7}  {state}")
    if ospf:
        out.append(f"{'OSPF ON':<20} {'NEIGHBOUR':<16} STATE")
        out.extend(f"{r['asset'][:20]:<20} {r['neighbour']:<16} {r['state']}" for r in ospf)
    for key, title in (("top_in", "IN"), ("top_out", "OUT")):
        rows = data.get(key) or []
        if rows:
            out.append(f"{'BUSIEST ' + title:<20} {'INTERFACE':<24} TRAFFIC")
            out.extend(f"{r['asset'][:20]:<20} {r['interface'][:24]:<24} {_bits(r['bps'])}" for r in rows)
    return ("\n".join(out) or "No SNMP interface, BGP or OSPF data yet.") + "\n"


def format_known_errors(data: dict) -> str:
    """Known errors: status, title, what matches, then the workaround (first line)."""
    items = data.get("items") or []
    if not items:
        return "No known error. Add them in Automation › Known errors or from an incident page.\n"
    out = []
    for k in items:
        match = " + ".join(x for x in (k.get("alertname"), f"title has '{k['pattern']}'" if k.get("pattern") else "") if x)
        out.append(f"[{k['status']}] #{k['id']} {k['title']}  ({match})")
        first = (k.get("workaround") or k.get("fix") or "").strip().splitlines()
        if first:
            out.append(f"    {first[0][:110]}")
    return "\n".join(out) + "\n"


def format_handover(rows: list[dict]) -> str:
    """The latest handover first: who, when, notes, open incidents, who took over."""
    if not rows:
        return "No handover yet. Write one: ./forgesre handover write \"notes\"\n"
    h = rows[0]
    out = [f"Handover #{h['id']} by {h['created_by']} at {str(h['created_at'])[:16]}",
           f"Taken over by {h['taken_by']} at {str(h['taken_at'])[:16]}" if h.get("taken_by") else "NOT TAKEN OVER YET: ./forgesre handover take",
           "", h.get("notes") or "(no notes)", "", f"Open incidents then ({len(h['open_incidents'])}):"]
    for i in h["open_incidents"][:20]:
        out.append(f"  {i['number']:<10} {str(i['severity']).upper():<9} {'acked ' if i.get('acked') else 'NOT ACKED'} {i['title']}")
    return "\n".join(out) + "\n"


def format_actions(rows: list[dict]) -> str:
    if not rows:
        return "No open follow-up actions.\n"
    out = [f"{'DUE':<11} {'OWNER':<24} {'INCIDENT':<10} ACTION"]
    for a in rows:
        due = (a["due"] or "-") + (" !" if a.get("overdue") else "")
        out.append(f"{due:<11} {(a['owner'] or '-')[:24]:<24} {a['incident'].split('_')[0]:<10} {a['text']}")
    return "\n".join(out) + "\n"


def format_sla(data: dict) -> str:
    if not data.get("enabled"):
        return "SLA timers are off (sla.enabled).\n"
    out = [f"Last {data['days']} days    response met/breached    resolve met/breached"]
    for sev, r in data["by_severity"].items():
        out.append(f"  {sev:<10} {r['response_met']:>12} / {r['response_breached']:<10} {r['resolve_met']:>10} / {r['resolve_breached']}")
    t = data.get("targets") or {}
    if t:
        out.append("Targets (min): " + ", ".join(f"{s} ack {t['response'][s]} resolve {t['resolve'][s]}" for s in ("critical", "high", "warning")))
    return "\n".join(out) + "\n"


def cmd_process(port: str, command: str, rest: list[str]) -> None:
    """./forgesre handover [write NOTES | take] · actions · sla [DAYS]"""
    jar, _me = ensure_jar(port)
    try:
        if command == "handover":
            if rest[:1] == ["write"]:
                post_json(port, jar, "/api/v1/handover", {"notes": " ".join(rest[1:])})
            elif rest[:1] == ["take"]:
                rows = get_json(port, jar, "/api/v1/handover").get("handovers") or []
                if not rows:
                    raise SystemExit("no handover to take over")
                post_json(port, jar, f"/api/v1/handover/{rows[0]['id']}/take", {})
            sys.stdout.write(format_handover(get_json(port, jar, "/api/v1/handover").get("handovers") or []))
        elif command == "actions":
            sys.stdout.write(format_actions(get_json(port, jar, "/api/v1/actions").get("actions") or []))
        else:
            days = rest[0] if rest and rest[0].isdigit() else "30"
            sys.stdout.write(format_sla(get_json(port, jar, f"/api/v1/sla?days={days}")))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"refused by ForgeSRE (exit {exc.returncode}); a handover is taken over by the next shift, not its writer") from None
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def cmd_slo(port: str) -> None:
    jar, _me = ensure_jar(port)
    try:
        sys.stdout.write(format_slos(list(get_json(port, jar, "/api/v1/slos").get("slos") or [])))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def cmd_checks(port: str, rest: list[str]) -> None:
    """./forgesre checks [run | ping NAME [EVERY_HOURS]]"""
    jar, _me = ensure_jar(port)
    try:
        if rest[:1] == ["run"]:
            post_json(port, jar, "/api/v1/checks/run", {})
            sys.stdout.write("Checks queued; results in a few seconds: ./forgesre checks\n")
        elif rest[:1] == ["ping"] and len(rest) >= 2:
            query = f"?every_hours={rest[2]}" if len(rest) >= 3 else ""
            post_json(port, jar, f"/api/v1/heartbeats/{rest[1]}{query}", {})
            sys.stdout.write(f"Heartbeat {rest[1]} recorded.\n")
        else:
            sys.stdout.write(format_checks(get_json(port, jar, "/api/v1/checks")))
    finally:
        if jar != SESSION_PATH and jar.exists():
            jar.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2:
        raise SystemExit("usage: cli_ops <port> <incidents|history|jobs|settings|set-secret|whoami|login|logout|numbers|assets|asset-refs|ping|probe|verify> [args]")
    port, command, *rest = argv
    if command == "incidents":
        cmd_incidents(port, rest)
    elif command == "history":
        cmd_history(port, rest)
    elif command == "numbers":
        cmd_numbers(port)
    elif command in {"assets", "inventory"}:
        cmd_assets(port, rest)
    elif command == "sources":
        cmd_sources(port)
    elif command == "discovery":
        cmd_discovery(port, rest)
    elif command == "capacity":
        cmd_capacity(port, rest)
    elif command == "metrics":
        cmd_metrics(port, rest)
    elif command == "security":
        cmd_security(port, rest)
    elif command == "zabbix":
        cmd_flow(port, ["zabbix", *(["check"] if not rest else (["check"] if rest[0] == "flow" else rest))])
    elif command == "flow":
        cmd_flow(port, rest)
    elif command in {"asset-refs", "asset_refs"}:
        cmd_asset_refs(port)
    elif command in {"ping", "probe"}:
        cmd_ping(port, rest)
    elif command == "verify":
        cmd_verify(port, rest)
    elif command == "whoami":
        cmd_whoami(port)
    elif command == "jobs":
        cmd_jobs(port)
    elif command == "checks":
        cmd_checks(port, rest)
    elif command == "slo":
        cmd_slo(port)
    elif command == "alert-quality":
        cmd_alert_quality(port, rest)
    elif command == "power":
        cmd_power(port)
    elif command in {"lifecycle", "racks", "synthetic", "network", "known-errors"}:
        cmd_lifecycle(port, command, rest)
    elif command in {"handover", "actions", "sla"}:
        cmd_process(port, command, rest)
    elif command == "forgeai":
        cmd_forgeai(port, rest)
    elif command == "kubernetes":
        cmd_k8s(port, rest)
    elif command == "settings":
        cmd_settings(port, rest)
    elif command == "set-secret":
        if len(rest) != 2:
            raise SystemExit("usage: cli_ops <port> set-secret <KEY> <secrets.env path>  (value on stdin)")
        write_secret(Path(rest[1]), rest[0], sys.stdin.readline())
    elif command == "logout":
        cmd_logout()
    elif command == "login":
        password = rest[1] if len(rest) >= 2 else os.environ.get("FORGESRE_CLI_PASSWORD", "")
        if not rest or not password:
            raise SystemExit("usage: cli_ops <port> login <email>  (password in FORGESRE_CLI_PASSWORD)")
        cmd_login(port, rest[0], password)
    else:
        raise SystemExit(f"unknown cli_ops command: {command}")


if __name__ == "__main__":
    main()
