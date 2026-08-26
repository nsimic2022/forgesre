#!/usr/bin/env python3
"""Live appliance verification. Writes a detailed report. Not pytest.

Run from the clone directory:

  ./forgesre test
  ./test.sh
  python3 scripts/appliance_test.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _yaml_scalar(value: str) -> str:
    text = value.strip().strip('"').strip("'")
    if " #" in f" {text}":
        text = text.split(" #", 1)[0].rstrip()
    return text.strip().strip('"').strip("'")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _yaml_email(text: str) -> dict[str, str]:
    """Tiny pull of notifications.email keys. Not a full YAML parser."""
    out = {"enabled": "", "host": "", "port": "", "from": "", "tls": ""}
    in_email = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "email:" and line.startswith("  email:"):
            in_email = True
            continue
        if in_email:
            indent = len(line) - len(line.lstrip(" "))
            if stripped and not stripped.startswith("#") and indent <= 2:
                break
            if indent == 4 and ":" in stripped and not stripped.startswith("#"):
                key, _, value = stripped.partition(":")
                key = key.strip()
                if key in out:
                    out[key] = _yaml_scalar(value)
    return out


def _yaml_ai(text: str) -> dict[str, str]:
    """Tiny pull of ai / ai.llm keys. Not a full YAML parser."""
    out = {"enabled": "", "mode": "", "url": "", "timeout_seconds": ""}
    in_ai = False
    in_llm = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ai:") and not line[:1].isspace():
            in_ai = True
            in_llm = False
            continue
        if in_ai and line and not line[:1].isspace() and not stripped.startswith("#"):
            break
        if not in_ai:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("enabled:") and indent == 2:
            out["enabled"] = _yaml_scalar(stripped.split(":", 1)[1])
            continue
        if stripped == "llm:" and indent == 2:
            in_llm = True
            continue
        if in_llm and indent <= 2 and stripped and stripped != "llm:" and not stripped.startswith("#"):
            in_llm = False
        if in_llm and indent == 4 and ":" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition(":")
            key = key.strip()
            if key in {"mode", "url", "timeout_seconds"}:
                out[key] = _yaml_scalar(value)
    return out


class Check:
    def __init__(self, name: str, status: str, detail: str, how: str = "", fix: str = ""):
        self.name = name
        self.status = status  # pass | warn | fail | skip
        self.detail = detail
        self.how = how
        self.fix = fix

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "how": self.how,
            "fix": self.fix,
        }


class Runner:
    def __init__(self, root: Path):
        self.root = root
        self.checks: list[Check] = []
        self.env = _load_env(root / ".env")
        self.secrets = _load_env(root / "secrets" / "secrets.env")
        self.port = self.env.get("FORGESRE_HTTP_PORT") or "8080"
        self.base = f"http://127.0.0.1:{self.port}"
        yml = root / "config" / "forgesre.yml"
        self.yaml_text = yml.read_text(encoding="utf-8", errors="replace") if yml.is_file() else ""
        self.email = _yaml_email(self.yaml_text)
        self.ai = _yaml_ai(self.yaml_text)
        self.cookie = ""

    def add(self, name: str, status: str, detail: str, how: str = "", fix: str = "") -> None:
        self.checks.append(Check(name, status, detail, how, fix))

    def run_cmd(self, argv: list[str], timeout: int = 20) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                argv,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, out.strip()
        except FileNotFoundError:
            return 127, f"command not found: {argv[0]}"
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    def http(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 8,
        cookie: str = "",
    ) -> tuple[int, str]:
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if cookie:
            req.add_header("Cookie", cookie)
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                body = resp.read(8000).decode("utf-8", errors="replace")
                return int(resp.status), body
        except urllib.error.HTTPError as exc:
            body = exc.read(2000).decode("utf-8", errors="replace") if exc.fp else ""
            return int(exc.code), body
        except Exception as exc:  # noqa: BLE001 — live probe
            return 0, str(exc)

    def docker(self) -> list[str]:
        code, _ = self.run_cmd(["docker", "info"])
        if code == 0:
            return ["docker"]
        code, _ = self.run_cmd(["sudo", "-n", "docker", "info"])
        if code == 0:
            return ["sudo", "docker"]
        return ["docker"]

    def docker_argv(self, *args: str) -> list[str]:
        base = self.docker()
        if base and base[0] == "sudo":
            return ["sudo", "-n", "docker", *args]
        return ["docker", *args]

    def compose(self, *args: str, timeout: int = 30) -> tuple[int, str]:
        base = self.docker()
        if base[0] == "sudo":
            argv = ["sudo", "-n", "docker", "compose", *args]
        else:
            argv = ["docker", "compose", *args]
        return self.run_cmd(argv, timeout=timeout)

    def login_cookie(self) -> str:
        email = self.secrets.get("FORGESRE_ADMIN_EMAIL", "")
        password = self.secrets.get("FORGESRE_ADMIN_PASSWORD", "")
        if not email or not password:
            return ""
        body = f"email={urllib_quote(email)}&password={urllib_quote(password)}".encode()
        req = urllib.request.Request(f"{self.base}/login", data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                raw = resp.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as exc:
            raw = (exc.headers.get("Set-Cookie") if exc.headers else "") or ""
        except Exception:
            return ""
        if "forgesre_session=" not in raw:
            return ""
        return raw.split(";", 1)[0]


def urllib_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def run_all(root: Path) -> Runner:
    r = Runner(root)

    # --- host ---
    code, out = r.run_cmd(["python3", "--version"])
    r.add("host.python3", "pass" if code == 0 else "fail", out or "python3 missing", "python3 --version", "apt install python3")
    code, out = r.run_cmd(["docker", "info"])
    if code != 0:
        code, out = r.run_cmd(["sudo", "-n", "docker", "info"])
    r.add(
        "host.docker",
        "pass" if code == 0 else "fail",
        "daemon reachable" if code == 0 else (out[-400:] or "docker info failed"),
        "docker info",
        "systemctl enable --now docker; usermod -aG docker \"$USER\" then re-login",
    )
    code, out = r.run_cmd(["docker", "compose", "version"])
    if code != 0:
        code, out = r.run_cmd(["sudo", "-n", "docker", "compose", "version"])
    r.add(
        "host.compose",
        "pass" if code == 0 else "fail",
        out.splitlines()[0] if out else "docker compose missing",
        "docker compose version",
        "apt install -y docker-compose-v2",
    )
    try:
        free = shutil_disk(root)
        status = "pass" if free >= 2 else "warn"
        r.add("host.disk_gb", status, f"{free:.1f} GB free on clone volume", "df -h .", "free disk if images cannot pull")
    except Exception as exc:
        r.add("host.disk_gb", "warn", str(exc))

    # --- files ---
    env_path = root / ".env"
    r.add(
        "files.env",
        "pass" if env_path.is_file() else "fail",
        str(env_path) if env_path.is_file() else "missing — run ./install.sh on a new VM only",
        "test -f .env",
        "New VM: ./install.sh. Existing: do not re-install; copy .env back from backup",
    )
    sec = root / "secrets" / "secrets.env"
    if not sec.is_file():
        r.add("files.secrets", "fail", "missing secrets/secrets.env", "test -f secrets/secrets.env", "./install.sh on a new VM only")
    else:
        mode = oct(sec.stat().st_mode & 0o777)
        status = "pass" if mode in {"0o600", "0o400"} else "warn"
        r.add("files.secrets", status, f"mode {mode} (want 600)", "stat secrets/secrets.env", "chmod 600 secrets/secrets.env && chmod 700 secrets")
    yml = root / "config" / "forgesre.yml"
    r.add(
        "files.forgesre_yml",
        "pass" if yml.is_file() else "fail",
        "present" if yml.is_file() else "missing config/forgesre.yml",
        "./forgesre config",
        "copy config/forgesre.example.yml → config/forgesre.yml",
    )
    gen = root / "data" / "generated"
    needed = ["prometheus.yml", "alertmanager.yml", "snmp.yml", "alerts.yml"]
    missing = [name for name in needed if not (gen / name).is_file()]
    r.add(
        "files.generated_monitoring",
        "pass" if not missing else "warn",
        "ok" if not missing else "missing " + ", ".join(missing),
        "./forgesre render-monitoring",
        "./forgesre render-monitoring && docker compose up -d",
    )

    sk = r.secrets.get("SECRET_KEY", "")
    tok = r.secrets.get("ALERTMANAGER_WEBHOOK_TOKEN", "")
    bad_sk = (not sk) or sk == "forgesre-dev-secret-change-me"
    bad_tok = (not tok) or tok in {"forgesre-dev-webhook-token", "CHANGE-ME-RENDER-MONITORING"}
    r.add(
        "secrets.refuse_defaults",
        "fail" if (bad_sk or bad_tok) else "pass",
        "SECRET_KEY / webhook token are not shipped defaults" if not (bad_sk or bad_tok) else "shipped default still set — Core refuses to start",
        "./forgesre secrets-check",
        "Put real values in secrets/secrets.env. Never FORGESRE_DEV=1 on a real DC",
    )

    # --- compose ---
    code, out = r.compose("ps", "--format", "json", timeout=25)
    running: list[str] = []
    if code == 0 and out:
        try:
            payload = parse_compose_ps(out)
            for row in payload:
                name = str(row.get("Service") or row.get("Name") or "")
                state = str(row.get("State") or row.get("Status") or "")
                if name:
                    running.append(f"{name}={state}")
        except json.JSONDecodeError:
            running = [line.strip() for line in out.splitlines()[:12]]
        r.add("compose.ps", "pass" if running else "warn", ", ".join(running) or out[:400], "docker compose ps", "docker compose up -d")
    else:
        r.add("compose.ps", "fail", out[-400:] or "compose ps failed", "docker compose ps", "systemctl start docker && docker compose up -d")

    code, out = r.compose("ps", "core", timeout=20)
    core_up = code == 0 and ("running" in out.lower() or "Up" in out)
    r.add(
        "compose.core",
        "pass" if core_up else "fail",
        "core running" if core_up else (out[-300:] or "core not running"),
        "docker compose ps core",
        "docker compose up -d --build core",
    )

    # --- HTTP stack ---
    probes = [
        ("http.core_health", f"{r.base}/api/v1/health", "curl -fsS http://127.0.0.1:%s/api/v1/health" % r.port, "docker compose logs --tail=100 core"),
        ("http.prometheus", "http://127.0.0.1:9090/-/ready", "curl -fsS http://127.0.0.1:9090/-/ready", "docker compose logs prometheus"),
        ("http.alertmanager", "http://127.0.0.1:9093/-/ready", "curl -fsS http://127.0.0.1:9093/-/ready", "docker compose logs alertmanager"),
        ("http.snmp_exporter", "http://127.0.0.1:9116/metrics", "curl -fsS http://127.0.0.1:9116/metrics", "docker compose up -d snmp-exporter"),
        ("http.loki", "http://127.0.0.1:3100/ready", "curl -fsS http://127.0.0.1:3100/ready", "docker compose logs loki"),
        ("http.alloy", "http://127.0.0.1:12345/metrics", "curl -fsS http://127.0.0.1:12345/metrics", "docker compose logs alloy"),
        ("http.grafana", "http://127.0.0.1:3000/api/health", "curl -fsS http://127.0.0.1:3000/api/health", "docker compose logs grafana"),
    ]
    for name, url, how, fix in probes:
        status_code, body = r.http(url)
        ok = 200 <= status_code < 400
        r.add(name, "pass" if ok else "fail", f"HTTP {status_code or 'down'} {body[:80].replace(chr(10), ' ')}", how, fix)

    nb_port = r.env.get("NETBOX_PORT") or "8001"
    nb_url = f"http://127.0.0.1:{nb_port}/login/"
    nb_code, nb_body = r.http(nb_url, timeout=5)
    if 200 <= nb_code < 400:
        r.add("http.netbox", "pass", f"HTTP {nb_code} :{nb_port}/login/", f"curl -fsS {nb_url}", "docker compose logs netbox")
    else:
        r.add(
            "http.netbox",
            "warn",
            f"HTTP {nb_code or 'down'} — first boot runs migrations; doctor stays yellow until /login/ answers",
            f"curl -fsS {nb_url}",
            "docker compose logs --tail=80 netbox   # wait; do not fake green",
        )

    llm_url = "http://127.0.0.1:8088/v1/models"
    code, _ = r.http(llm_url)
    profiles = r.env.get("COMPOSE_PROFILES", "")
    profile_ai = "ai" in [p.strip() for p in profiles.split(",") if p.strip()]
    if profile_ai or code == 200:
        r.add(
            "http.llm",
            "pass" if code == 200 else "warn",
            f"HTTP {code or 'down'} (COMPOSE_PROFILES={profiles or 'empty'})",
            "curl -sS http://127.0.0.1:8088/v1/models",
            "docker compose --profile ai up -d llm   or   ./forgesre fetch-llm",
        )
    else:
        r.add("http.llm", "skip", "llama.cpp not in COMPOSE_PROFILES and :8088 is closed — ForgeRCA still runs", "./forgesre fetch-llm")

    ai = r.ai
    ai_on = ai.get("enabled", "").lower() in {"true", "yes", "1"} and (ai.get("mode") or "") != "disabled"
    if not ai_on:
        r.add(
            "yaml.ai",
            "skip",
            f"ai.enabled={ai.get('enabled') or 'missing'} mode={ai.get('mode') or 'missing'} — ForgeRCA only",
            "./forgesre config",
            "./forgesre fetch-llm",
        )
    else:
        url = ai.get("url") or ""
        r.add(
            "yaml.ai",
            "pass" if url else "warn",
            f"enabled mode={ai.get('mode')} url={url} timeout={ai.get('timeout_seconds') or '600'}",
            "grep -nE 'llm|8088' config/forgesre.yml",
            "set ai.llm.url then docker compose up -d --force-recreate core",
        )

    data_dir = Path(r.env.get("FORGESRE_DATA") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    gguf = data_dir / "models" / "model.gguf"
    want_gguf = profile_ai or (ai.get("mode") == "bundled")
    if gguf.is_file():
        size = gguf.stat().st_size
        gb = size / (1024**3)
        r.add(
            "files.gguf",
            "pass" if size > 1_000_000_000 else "fail",
            f"{gguf} ({gb:.1f} GB)",
            f"ls -lh {gguf}",
            "./forgesre fetch-llm",
        )
    elif want_gguf:
        r.add("files.gguf", "fail", f"missing {gguf}", f"ls -lh {gguf}", "./forgesre fetch-llm")
    else:
        r.add("files.gguf", "skip", "no GGUF and profile ai is off", "./forgesre fetch-llm")

    if profile_ai or code == 200:
        cid_rc, cid_out = r.compose("ps", "-q", "llm")
        cid = cid_out.strip().splitlines()[0] if cid_rc == 0 and cid_out.strip() else ""
        if not cid:
            r.add(
                "compose.llm_health",
                "warn",
                "no llm container id",
                "docker compose ps llm",
                "docker compose --profile ai up -d llm",
            )
        else:
            ic, iout = r.run_cmd(
                r.docker_argv("inspect", "--format", "{{json .State.Health}}", cid)
            )
            health_status = "none"
            if ic == 0 and iout and iout not in {"<no value>", "null"}:
                try:
                    health = json.loads(iout)
                    if isinstance(health, dict):
                        health_status = str(health.get("Status") or "none")
                except json.JSONDecodeError:
                    health_status = "unparsed"
            mark = "pass" if health_status == "healthy" else ("warn" if health_status in {"starting", "none"} else "fail")
            r.add(
                "compose.llm_health",
                mark,
                f"Status={health_status} {iout[:160]}",
                "docker compose ps -q llm | xargs -r docker inspect --format='{{json .State.Health}}'",
                "docker compose logs --tail=100 llm",
            )
        log_rc, log_out = r.compose("logs", "--tail", "80", "llm", timeout=25)
        if log_rc == 0:
            lowered = log_out.lower()
            hits = sum(lowered.count(word) for word in ("error", "failed", "fatal"))
            r.add(
                "logs.llm_errors",
                "warn" if hits else "pass",
                f"last 80 lines: {hits} error/failed/fatal hits",
                "docker compose logs --tail=100 llm",
                "docker compose logs -f llm",
            )
        else:
            r.add("logs.llm_errors", "skip", "could not read llm logs", "docker compose logs llm")
    else:
        r.add("compose.llm_health", "skip", "profile ai off and :8088 closed")
        r.add("logs.llm_errors", "skip", "profile ai off")

    rc_port = r.env.get("ROUNDCUBE_PORT") or "8081"
    if "mailbox" in profiles.split(","):
        code, body = r.http(f"http://127.0.0.1:{rc_port}/")
        r.add("http.roundcube", "pass" if code else "warn", f"HTTP {code or 'down'} on :{rc_port}", f"curl -I http://127.0.0.1:{rc_port}/", "./forgesre mailbox")
        sock_ok = _port_open("127.0.0.1", 587)
        r.add("mailbox.submission_587", "pass" if sock_ok else "warn", "127.0.0.1:587 open" if sock_ok else "587 closed", "ss -lnt | grep 587", "./forgesre mailbox")
    else:
        r.add("http.roundcube", "skip", "Compose profile mailbox is off (Gmail/Outlook SMTP still valid)", "./forgesre mailbox  # later, own domain")

    # --- doctor API ---
    token = r.secrets.get("ALERTMANAGER_WEBHOOK_TOKEN", "")
    code, body = r.http(f"{r.base}/api/v1/system/doctor", headers={"Authorization": f"Bearer {token}"} if token else None)
    if code == 200:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        overall = str(payload.get("overall") or "?")
        comps = payload.get("components") or {}
        failed = payload.get("failed") or []
        bits = ", ".join(f"{k}={v.get('status')}" for k, v in comps.items())
        r.add(
            "api.doctor",
            "pass" if overall == "HEALTHY" else "warn",
            f"{overall}; {bits}",
            "./forgesre doctor",
            "Fix failed components: " + ", ".join(failed) if failed else "",
        )
    else:
        r.add("api.doctor", "fail", f"HTTP {code} {body[:120]}", "./forgesre doctor", "./forgesre secrets-check && docker compose logs core")

    # --- login + product APIs ---
    cookie = r.login_cookie()
    if cookie:
        r.cookie = cookie
        r.add("api.login", "pass", "session cookie from install admin", "POST /login", "")
        for name, path in [
            ("api.assets", "/api/v1/assets"),
            ("api.incidents", "/api/v1/incidents"),
            ("api.history", "/api/v1/history?days=90"),
            ("api.jobs", "/api/v1/jobs"),
            ("api.journal", "/api/v1/journal?limit=20"),
        ]:
            status_code, body = r.http(f"{r.base}{path}", cookie=cookie)
            r.add(name, "pass" if status_code == 200 else "fail", f"HTTP {status_code}", f"curl -b cookie {r.base}{path}", "docker compose logs core")
        status_code, body = r.http(f"{r.base}/admin", cookie=cookie)
        r.add("ui.admin", "pass" if status_code == 200 and "Users" in body else "warn", f"HTTP {status_code}", f"open {r.base}/admin", "")
        status_code, body = r.http(f"{r.base}/ops", cookie=cookie)
        r.add("ui.ops", "pass" if status_code == 200 else "warn", f"HTTP {status_code}", f"open {r.base}/ops", "")
    else:
        r.add("api.login", "fail", "could not sign in as FORGESRE_ADMIN_*", "curl -c jar -d email=… /login", "check secrets/secrets.env; do not re-run install.sh")

    if token:
        for name, path in [
            ("api.sd_prometheus", "/api/v1/sd/prometheus"),
            ("api.sd_snmp", "/api/v1/sd/snmp"),
        ]:
            status_code, body = r.http(f"{r.base}{path}", headers={"Authorization": f"Bearer {token}"})
            r.add(name, "pass" if status_code == 200 else "fail", f"HTTP {status_code} {body[:80]}", f"curl -H 'Authorization: Bearer …' {r.base}{path}", "./forgesre sd")

    # --- email (config only; does not send) ---
    enabled = r.email.get("enabled", "").lower() in {"true", "yes", "1"}
    host = r.email.get("host", "")
    if not enabled:
        r.add("email.yaml", "skip", "notifications.email.enabled is false — outbox stays generated", "edit config/forgesre.yml then recreate core", "")
    else:
        kind = "other"
        low = host.lower()
        if "gmail" in low:
            kind = "gmail"
        elif "office365" in low or "outlook" in low:
            kind = "outlook"
        elif low in {"127.0.0.1", "localhost"}:
            kind = "local-mailbox" if r.email.get("port") == "587" else "local"
        has_user = bool(r.secrets.get("SMTP_USERNAME"))
        r.add(
            "email.yaml",
            "pass" if host and has_user else "warn",
            f"enabled host={host} port={r.email.get('port')} tls={r.email.get('tls')} provider={kind} SMTP_USERNAME={'set' if has_user else 'empty'}",
            "notifications.email in config/forgesre.yml + SMTP_* in secrets",
            "Gmail: smtp.gmail.com:587. Outlook: smtp.office365.com:587. Then docker compose up -d --force-recreate core",
        )

    # --- CLI smoke ---
    code, out = r.run_cmd(["bash", str(root / "scripts" / "forgesre"), "version"])
    r.add("cli.version", "pass" if code == 0 else "warn", out or r.env.get("FORGESRE_VERSION", "?"), "./forgesre version")
    code, out = r.run_cmd(["bash", str(root / "scripts" / "forgesre"), "help"])
    r.add("cli.help", "pass" if code == 0 and "doctor" in out else "fail", "help lists doctor/test" if "test" in out else out[:120], "./forgesre help")

    # --- core logs (errors) ---
    code, out = r.compose("logs", "--tail", "80", "core", timeout=25)
    if code == 0:
        lowered = out.lower()
        hits = sum(lowered.count(word) for word in ("traceback", "exception", "error"))
        r.add(
            "logs.core_errors",
            "warn" if hits else "pass",
            f"last 80 lines: {hits} error/exception/traceback hits",
            "docker compose logs --tail=100 core | grep -iE 'error|exception'",
            "docker compose logs -f core",
        )
    else:
        r.add("logs.core_errors", "skip", "could not read core logs", "docker compose logs core")

    return r


def parse_compose_ps(out: str) -> list[dict[str, Any]]:
    """docker compose ps --format json: JSON array or NDJSON (one object per line)."""
    text = (out or "").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
        elif isinstance(item, list):
            rows.extend(row for row in item if isinstance(row, dict))
    return rows


def shutil_disk(root: Path) -> float:
    import shutil

    usage = shutil.disk_usage(root)
    return usage.free / (1024**3)


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def counts(checks: list[Check]) -> dict[str, int]:
    tally = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for item in checks:
        tally[item.status] = tally.get(item.status, 0) + 1
    return tally


def render_markdown(r: Runner) -> str:
    tally = counts(r.checks)
    overall = "FAIL" if tally["fail"] else ("WARN" if tally["warn"] else "PASS")
    lines = [
        "# ForgeSRE appliance test report",
        "",
        f"- Generated: `{_now()}`",
        f"- Host: `{socket.gethostname()}`",
        f"- Clone: `{r.root}`",
        f"- UI: `{r.base}`",
        f"- Version: `{r.env.get('FORGESRE_VERSION') or 'unknown'}`",
        f"- COMPOSE_PROFILES: `{r.env.get('COMPOSE_PROFILES') or '(empty)'}`",
        f"- Overall: **{overall}**  (pass {tally['pass']} · warn {tally['warn']} · fail {tally['fail']} · skip {tally['skip']})",
        "",
        "`./forgesre doctor` is the short health light. This report is the long verification (files, compose, HTTP, login, APIs, email config, logs).",
        "",
        "| Status | Check | Detail | How to test | Fix |",
        "|---|---|---|---|---|",
    ]
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    for item in r.checks:
        detail = item.detail.replace("|", "\\|").replace("\n", " ")
        how = item.how.replace("|", "\\|")
        fix = item.fix.replace("|", "\\|")
        lines.append(f"| {mark.get(item.status, item.status)} | `{item.name}` | {detail} | `{how}` | {fix} |")
    lines += [
        "",
        "## Notes",
        "",
        "- SKIP means the feature is off on purpose (LLM, mailbox, SMTP disabled).",
        "- WARN is degraded but the appliance can still run incidents.",
        "- FAIL needs a fix before you trust production send/RCA.",
        "- This command does **not** send email and does **not** run `./install.sh`.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ForgeSRE live appliance test (detailed report).")
    parser.add_argument("--json", action="store_true", help="also print JSON to stdout")
    parser.add_argument("--out", default="", help="write Markdown report here (default data/reports/…)")
    parser.add_argument("--quiet", action="store_true", help="no Markdown on stdout (still writes --out)")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    runner = run_all(ROOT)
    md = render_markdown(runner)
    tally = counts(runner.checks)
    overall = "FAIL" if tally["fail"] else ("WARN" if tally["warn"] else "PASS")

    out_path = Path(args.out) if args.out else ROOT / "data" / "reports" / time.strftime("forgesre-test-%Y%m%d-%H%M%S.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    json_path = out_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "overall": overall,
                "counts": tally,
                "generated": _now(),
                "host": socket.gethostname(),
                "port": runner.port,
                "checks": [c.as_dict() for c in runner.checks],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if not args.quiet:
        sys.stdout.write(md)
        sys.stdout.write(f"\nWrote {out_path}\nWrote {json_path}\n")
    if args.json:
        sys.stdout.write(json_path.read_text(encoding="utf-8"))

    return 1 if tally["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
