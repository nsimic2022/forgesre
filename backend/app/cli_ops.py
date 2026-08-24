"""Host CLI helpers: cookie session + incident/history printers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from app.asset_probe import (
    DEFAULT_TIMEOUT,
    ad_hoc_item,
    format_report,
    looks_like_host,
    overall_exit,
    probe_target,
    select_assets,
)
from app.cli_view import color_enabled, format_board, format_detail, format_history_rows
from app.seed import is_demo_asset_id

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


def _curl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _login(port: str, jar: Path, email: str, password: str) -> None:
    jar.parent.mkdir(parents=True, exist_ok=True)
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
            "-d",
            f"email={email}&password={password}",
        ]
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
        ["curl", "-fsS", "-c", str(jar), "-b", str(jar), f"http://127.0.0.1:{port}{path}"]
    )
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
    return is_demo_asset_id(item.get("asset_id") or item.get("hostname"))


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
                raise SystemExit("usage: ./forgesre ping [--timeout seconds] [--demo] [asset-id-or-ip]")
            if timeout <= 0:
                raise SystemExit("--timeout must be > 0")
            i += 2
            continue
        if item in {"--demo", "--include-demo"}:
            include_demo = True
            i += 1
            continue
        if item in {"-h", "--help"}:
            raise SystemExit("usage: ./forgesre ping [--timeout seconds] [--demo] [asset-id-or-ip]")
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
        raise SystemExit(f"no asset matching {selector!r}. Try an asset id, hostname, or IP.")
    if not chosen:
        print("No assets with an IP to probe.")
        if skipped_demo:
            print(f"skipped {skipped_demo} demo lab asset(s). Include with: ./forgesre ping --demo")
        print("Add a host under Assets, then rerun ./forgesre ping")
        raise SystemExit(0)

    results = [probe_target(item, timeout=timeout) for item in chosen]
    sys.stdout.write(format_report(results, timeout=timeout, skipped_demo=skipped_demo, color=color_enabled()))
    raise SystemExit(overall_exit(results))


def main(argv: list[str] | None = None) -> None:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2:
        raise SystemExit("usage: cli_ops <port> <incidents|history|whoami|login|logout|numbers|ping|probe> [args]")
    port, command, *rest = argv
    if command == "incidents":
        cmd_incidents(port, rest)
    elif command == "history":
        cmd_history(port, rest)
    elif command == "numbers":
        cmd_numbers(port)
    elif command in {"ping", "probe"}:
        cmd_ping(port, rest)
    elif command == "whoami":
        cmd_whoami(port)
    elif command == "logout":
        cmd_logout()
    elif command == "login":
        if len(rest) < 2:
            raise SystemExit("usage: cli_ops <port> login <email> <password>")
        cmd_login(port, rest[0], rest[1])
    else:
        raise SystemExit(f"unknown cli_ops command: {command}")


if __name__ == "__main__":
    main()
