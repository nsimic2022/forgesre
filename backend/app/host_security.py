"""Read-only security check of the ForgeSRE host (./forgesre security, doctor, Settings › Security).

Runs on the host, from the clone directory, with the standard library only. It changes nothing:
it reads the SSH server config, the firewall state, the listening ports, file permissions and the
update state, and says for each finding what to do by hand. Every outside command has a timeout;
a check that cannot run (no root, command missing) is reported as "unknown", never as a failure.

The report is written to data/generated/security-host.json so Core can show it in Settings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ports that must only listen on 127.0.0.1: databases, Prometheus stack, LLM, Docker read-only proxy.
LOCAL_ONLY = {
    5432: "Postgres", 9090: "Prometheus", 9093: "Alertmanager", 9116: "snmp_exporter", 9115: "blackbox_exporter",
    3100: "Loki", 12345: "Alloy UI", 2377: "docker-ro (Docker API, read-only)", 6379: "NetBox Redis",
    6380: "LibreNMS Redis", 3307: "LibreNMS MariaDB", 8088: "local LLM (llama.cpp)", 587: "mailbox submission",
}
# Linux system services that commonly listen and are not ForgeSRE's business.
SYSTEM_PORTS = {68: "DHCP client", 546: "DHCPv6 client", 323: "chrony", 5353: "mDNS"}


def check(key: str, label: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    """One finding. status: ok, warn, bad, info or unknown (could not be checked)."""
    return {"key": key, "label": label, "status": status, "detail": detail, "fix": fix}


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a read-only command; (exit code, stdout+stderr). 127 when missing, 124 on timeout."""
    if not shutil.which(cmd[0]):
        return 127, ""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 126, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


# --- SSH ---------------------------------------------------------------------------------------

def sshd_settings(text: str, read_include: Any = None) -> dict[str, str]:
    """Effective sshd keywords from a config text: the first value wins (as sshd does), Include
    files are read in place, and everything after the first Match block is ignored."""
    out: dict[str, str] = {}

    def walk(body: str) -> bool:
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split(None, 1)
            key, value = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
            if key == "match":
                return False
            if key == "include" and read_include is not None:
                for name in value.split():
                    for included in read_include(name):
                        if not walk(included):
                            return False
                continue
            out.setdefault(key, value.lower())
        return True

    walk(text)
    return out


def _read_include(name: str) -> list[str]:
    import glob

    base = Path("/etc/ssh")
    pattern = name if name.startswith("/") else str(base / name)
    texts = []
    for path in sorted(glob.glob(pattern)):
        try:
            texts.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def ssh_checks(settings: dict[str, str] | None) -> list[dict[str, str]]:
    """Password login and root login. ``settings`` None means the config could not be read."""
    if settings is None:
        return [check("ssh", "SSH server", "unknown", "Could not read /etc/ssh/sshd_config.", "Run: sudo ./forgesre security")]
    if settings.get("_missing"):
        return [check("ssh", "SSH server", "info", "No SSH server is installed on this host.")]
    drop_in = "/etc/ssh/sshd_config.d/10-forgesre.conf"  # before 50-cloud-init.conf: the first value wins
    out = []
    password = settings.get("passwordauthentication", "yes")
    kbd = settings.get("kbdinteractiveauthentication", settings.get("challengeresponseauthentication", "no"))
    if password == "no" and kbd == "no":
        out.append(check("ssh_password", "SSH password login", "ok", "Only keys can log in."))
    else:
        out.append(check("ssh_password", "SSH password login", "warn", "SSH accepts passwords, so it can be guessed.",
                         f"First log in with a key (ssh-copy-id), then put 'PasswordAuthentication no' and "
                         f"'KbdInteractiveAuthentication no' in {drop_in}; check with 'sudo sshd -t' and "
                         "'sudo systemctl reload ssh'. Keep this session open until a new login works."))
    root = settings.get("permitrootlogin", "prohibit-password")
    if root == "no":
        out.append(check("ssh_root", "SSH root login", "ok", "root cannot log in over SSH."))
    elif root in {"prohibit-password", "without-password", "forced-commands-only"}:
        out.append(check("ssh_root", "SSH root login", "info", "root can log in with a key only.",
                         f"Optional: 'PermitRootLogin no' in {drop_in}, then use sudo."))
    else:
        out.append(check("ssh_root", "SSH root login", "bad", "root can log in over SSH with a password.",
                         f"Put 'PermitRootLogin no' in {drop_in}; 'sudo sshd -t' and 'sudo systemctl reload ssh'."))
    return out


# --- Firewall ----------------------------------------------------------------------------------

def firewall_check(ufw_out: tuple[int, str], nft_out: tuple[int, str], firewalld_out: tuple[int, str]) -> dict[str, str]:
    """Whether some firewall filters inbound traffic: ufw, firewalld or an nftables input chain that drops."""
    fix = "See docs/install-config.md › Hardening the server: ufw with the ForgeSRE ports."
    code, text = ufw_out
    if code == 0 and re.search(r"^Status:\s*active", text, re.MULTILINE):
        allowed = sorted(set(re.findall(r"^(\d+(?:/\w+)?|OpenSSH)\b[^\n]*ALLOW", text, re.MULTILINE)))
        return check("firewall", "Firewall", "ok", "ufw is active" + (f"; allowed: {', '.join(allowed)}" if allowed else "") + ".")
    code_fd, text_fd = firewalld_out
    if code_fd == 0 and text_fd.strip() == "running":
        return check("firewall", "Firewall", "ok", "firewalld is running.")
    code_n, text_n = nft_out
    if code_n == 0 and re.search(r"hook input[^\n]*policy drop", text_n):
        return check("firewall", "Firewall", "ok", "nftables drops inbound traffic by default.")
    if code == 0 and "inactive" in text:
        return check("firewall", "Firewall", "warn", "ufw is installed but off: every listening port is reachable from the network.", fix)
    if (code in {1, 126} or code_n in {1, 126}) and os.geteuid() != 0:
        return check("firewall", "Firewall", "unknown", "Reading the firewall needs root.", "Run: sudo ./forgesre security")
    return check("firewall", "Firewall", "warn", "No firewall found: every listening port is reachable from the network.", fix)


def ufw_allowed(text: str) -> set[tuple[int, str]] | None:
    """Ports ufw lets in, as (port, proto) with proto "" for both; None when ufw is not active.
    Rules limited to a source ("from 10.0.0.0/24") count as allowed: the port is reachable from there."""
    if not re.search(r"^Status:\s*active", text, re.MULTILINE):
        return None
    out: set[tuple[int, str]] = set()
    for line in text.splitlines():
        if "ALLOW" not in line or "ALLOW OUT" in line:
            continue
        first = line.split()[0] if line.split() else ""
        if first == "OpenSSH":
            out.add((22, "tcp"))
            continue
        m = re.match(r"^(\d+)(?::(\d+))?(?:/(tcp|udp))?$", first)
        if m:
            low, high = int(m.group(1)), int(m.group(2) or m.group(1))
            for port in range(low, min(high, low + 1000) + 1):
                out.add((port, m.group(3) or ""))
    return out


# --- Listening ports ---------------------------------------------------------------------------

def listeners(ss_text: str) -> list[tuple[str, str, int]]:
    """(protocol, address, port) from `ss -Hlntu`, e.g. ("tcp", "0.0.0.0", 8080)."""
    out = []
    for line in ss_text.splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        proto, local = cols[0], cols[4]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        addr = addr.strip("[]").split("%", 1)[0]
        out.append((proto, addr, int(port)))
    return out


# ForgeSRE programs by process name (as ss shows it, at most 15 characters), for ports that are not
# in the fixed list: their sockets belong to the platform even when the port number is unusual.
FORGESRE_PROCS = {
    "snmp_exporter": "SNMP polling (snmp_exporter)", "alloy": "Alloy (syslog, logs)", "prometheus": "Prometheus",
    "alertmanager": "Alertmanager", "grafana": "Grafana", "grafana-server": "Grafana", "loki": "Loki",
    "blackbox_export": "blackbox_exporter (ping, HTTP checks)", "granian": "NetBox", "llama-server": "local LLM",
    "caddy": "HTTPS proxy", "uvicorn": "ForgeSRE Core",
}


def listener_procs(ss_text: str) -> dict[tuple[str, int], str]:
    """Process name per (protocol, port) from `ss -Hlntup` (root sees every process)."""
    out: dict[tuple[str, int], str] = {}
    for line in ss_text.splitlines():
        cols = line.split()
        match = re.search(r'users:\(\("([^"]+)"', line)
        if len(cols) < 5 or not match:
            continue
        port = cols[4].rpartition(":")[2]
        if port.isdigit():
            out.setdefault((cols[0], int(port)), match.group(1))
    return out


def _loopback(addr: str) -> bool:
    return addr.startswith("127.") or addr in {"::1", "localhost"}


def expected_public(env: dict[str, str]) -> dict[int, str]:
    """Ports ForgeSRE itself opens to the network with this .env."""
    def port(key: str, default: int) -> int:
        try:
            return int(str(env.get(key) or default).strip())
        except ValueError:
            return default

    https = str(env.get("FORGESRE_HTTPS") or "off").lower()
    ports = {22: "SSH", port("GRAFANA_PORT", 3000): "Grafana", port("NETBOX_PORT", 8001): "NetBox",
             8000: "LibreNMS", 514: "device syslog (Alloy)", 162: "SNMP traps", 25: "mailbox SMTP",
             993: "mailbox IMAP", port("ROUNDCUBE_PORT", 8081): "Roundcube"}
    if str(env.get("FORGESRE_BIND") or "").strip() != "127.0.0.1":
        ports[port("FORGESRE_HTTP_PORT", 8080)] = "ForgeSRE UI and webhooks"
    if https in {"on", "only", "1", "true", "yes"}:
        ports[port("FORGESRE_HTTPS_PORT", 443)] = "ForgeSRE HTTPS"
        ports[80] = "HTTP → HTTPS redirect"
    for extra in re.split(r"[,\s]+", str(env.get("FORGESRE_SECURITY_ALLOW_PORTS") or "")):
        if extra.isdigit():
            ports[int(extra)] = "allowed in FORGESRE_SECURITY_ALLOW_PORTS"
    return ports


def port_checks(rows: list[tuple[str, str, int]] | None, env: dict[str, str], ephemeral_from: int = 32768,
                allowed: set[tuple[int, str]] | None = None, procs: dict[tuple[str, int], str] | None = None) -> list[dict[str, str]]:
    """Compare what listens on the network with what ForgeSRE needs: local-only services open to
    the network are bad; unknown open ports are warnings; random high UDP ports (client sockets)
    are ignored. ``allowed`` (from ufw) lowers a finding when the firewall blocks that port."""

    def through(port: int, proto: str) -> bool:
        return allowed is None or (port, proto) in allowed or (port, "") in allowed
    if rows is None:
        return [check("ports", "Open ports", "unknown", "Could not list listening ports (ss is missing).", "Install iproute2.")]
    public = expected_public(env)
    exposed, exposed_blocked, unknown, unknown_blocked, known, known_blocked = [], [], [], [], set(), set()
    for proto, addr, port in rows:
        if _loopback(addr) or (addr.startswith("127.0.0.53") or addr == "127.0.0.54"):
            continue
        open_ = through(port, proto)
        name = (procs or {}).get((proto, port), "")
        if port not in LOCAL_ONLY and port not in public and name in FORGESRE_PROCS:
            # A ForgeSRE program on a port outside the list (e.g. snmp_exporter's UDP socket on 161):
            # replies to its own requests come back without a firewall rule.
            (known if open_ else known_blocked).add(f"{port}/{proto} {FORGESRE_PROCS[name]}")
            continue
        if port in LOCAL_ONLY:
            (exposed if open_ else exposed_blocked).append(f"{port}/{proto} {LOCAL_ONLY[port]}")
        elif port in public:
            (known if open_ else known_blocked).add(f"{port}/{proto} {public[port]}")
        elif port in SYSTEM_PORTS or (proto == "udp" and port >= ephemeral_from):
            continue
        else:
            (unknown if open_ else unknown_blocked).append(f"{port}/{proto} on {addr or '*'}" + (f" ({name})" if name else ""))
    out = []
    fix_bind = "Bind them to 127.0.0.1 again (docker-compose.yml was changed?) and block them in the firewall."
    if exposed:
        out.append(check("ports_local", "Local-only services", "bad",
                         "Open to the network but meant for this host only: " + "; ".join(sorted(set(exposed))) + ".", fix_bind))
    elif exposed_blocked:
        out.append(check("ports_local", "Local-only services", "warn",
                         "Listening on all addresses although meant for this host only; the firewall blocks them now: "
                         + "; ".join(sorted(set(exposed_blocked))) + ".", fix_bind))
    else:
        out.append(check("ports_local", "Local-only services", "ok", "Databases, Prometheus, Loki, LLM and docker-ro listen on 127.0.0.1 only."))
    fix_other = ("Find the program: sudo ss -lntup | grep ':PORT '. Stop it, or make it listen on 127.0.0.1. "
                 "If it is needed from the network, list it in .env FORGESRE_SECURITY_ALLOW_PORTS=…")
    if unknown:
        out.append(check("ports_other", "Other open ports", "warn",
                         "Reachable from the network, not used by ForgeSRE: " + "; ".join(sorted(set(unknown))) + ".", fix_other))
    if unknown_blocked:
        out.append(check("ports_blocked", "Other ports, blocked", "info",
                         "Listening, not used by ForgeSRE, but the firewall blocks them: " + "; ".join(sorted(set(unknown_blocked))) + ".",
                         fix_other))
    if allowed is None:
        text = ("Open to the network: " + "; ".join(sorted(known)) + ".") if known else "No ForgeSRE port is open to the network."
    else:
        text = (("Reachable through the firewall: " + "; ".join(sorted(known)) + ". ") if known else "No ForgeSRE port is let in by the firewall. ")
        if known_blocked:
            text += "Blocked by the firewall (usable on this machine only): " + "; ".join(sorted(known_blocked)) + "."
    out.append(check("ports_public", "ForgeSRE ports", "info", text.strip(),
                     "Allow the ones people and devices need, only from their networks: docs/install-config.md › Hardening the server."))
    https = str(env.get("FORGESRE_HTTPS") or "off").lower()
    http_port = int(env.get("FORGESRE_HTTP_PORT") or 8080)
    plain = expected_public(env).get(http_port) and through(http_port, "tcp")
    if https in {"on", "1", "true", "yes"} and plain:
        out.append(check("https_bypass", "Plain HTTP next to HTTPS", "warn",
                         f"HTTPS is on, but the UI also answers plain HTTP on :{env.get('FORGESRE_HTTP_PORT') or 8080}, so passwords can travel unencrypted.",
                         "./forgesre https only (agents and webhooks then use HTTPS too), or allow that port only from the monitoring hosts."))
    return out


# --- Files -------------------------------------------------------------------------------------

def mode_checks(root: Path, data: Path) -> list[dict[str, str]]:
    """Secrets, install report and backups must not be readable by other users."""
    wanted = [
        (root / "secrets", 0o700, "secrets folder"), (root / "secrets" / "secrets.env", 0o600, "secrets.env"),
        (root / "installation-report.md", 0o600, "installation report"), (data / "backups", 0o700, "backups folder"),
    ]
    loose = []
    for path, limit, label in wanted:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            continue
        except OSError:
            loose.append(f"{label}: cannot read")
            continue
        if mode & ~limit & 0o077:
            loose.append(f"{label} {oct(mode)[2:]} (want {oct(limit)[2:]}): chmod {oct(limit)[2:]} {path}")
    if loose:
        return [check("files", "Secret files", "bad", "Readable by other users: " + "; ".join(loose) + ".",
                      "Run the chmod shown for each file.")]
    return [check("files", "Secret files", "ok", "secrets, installation report and backups are readable by their owner only.")]


# --- Updates and services ----------------------------------------------------------------------

def update_checks(reboot_file: Path, apt_check: tuple[int, str], auto_upgrades: str | None) -> list[dict[str, str]]:
    """Pending security updates (Ubuntu apt-check), a pending reboot, and unattended-upgrades."""
    out = []
    code, text = apt_check
    match = re.search(r"(\d+);(\d+)", text)
    if code == 0 and match:
        security = int(match.group(2))
        out.append(check("updates", "Security updates", "warn" if security else "ok",
                         f"{security} security update(s) waiting." if security else "No security update is waiting.",
                         "sudo apt-get update && sudo apt-get upgrade, then ./forgesre doctor" if security else ""))
    else:
        out.append(check("updates", "Security updates", "unknown", "Could not count them (not Ubuntu, or apt-check is missing)."))
    if reboot_file.exists():
        try:
            pkgs = (reboot_file.parent / "reboot-required.pkgs").read_text(encoding="utf-8").split()
        except OSError:
            pkgs = []
        out.append(check("reboot", "Restart needed", "warn", "The system asks for a restart" + (f" ({', '.join(sorted(set(pkgs))[:5])})" if pkgs else "") + ".",
                         "Restart at a quiet time: sudo reboot. ForgeSRE comes back by itself; then ./forgesre doctor."))
    if auto_upgrades is not None:
        on = re.search(r'Unattended-Upgrade\s+"1"', auto_upgrades) is not None
        out.append(check("auto_updates", "Automatic security updates", "ok" if on else "warn",
                         "unattended-upgrades installs security fixes daily." if on else "Security fixes are not installed automatically.",
                         "" if on else "sudo apt-get install unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades"))
    return out


def fail2ban_check(state: tuple[int, str]) -> dict[str, str]:
    code, text = state
    if code == 0 and text.strip() == "active":
        return check("fail2ban", "fail2ban", "ok", "Blocks addresses that keep guessing SSH passwords.")
    return check("fail2ban", "fail2ban", "info", "Not running: repeated SSH guesses are not blocked.",
                 "sudo apt-get install fail2ban (its default SSH jail is enough).")


# --- Collect -----------------------------------------------------------------------------------

def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def collect(root: Path) -> dict[str, Any]:
    """Run every host check from the clone directory ``root``; returns the report (also for JSON)."""
    env = _dotenv(root / ".env")
    data = Path(env.get("FORGESRE_DATA") or "./data")
    data = data if data.is_absolute() else (root / data)
    try:
        sshd = sshd_settings(Path("/etc/ssh/sshd_config").read_text(encoding="utf-8", errors="replace"), _read_include)
    except FileNotFoundError:
        sshd = {"_missing": "yes"}  # no SSH server installed: nothing to log in to
    except OSError:
        sshd = None
    code, ss_text = _run(["ss", "-Hlntup"])  # -p: process names, complete only as root
    try:
        low = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        low = 32768
    auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    ufw = _run(["ufw", "status"])
    checks = [
        *ssh_checks(sshd),
        firewall_check(ufw, _run(["nft", "list", "chains"]), _run(["firewall-cmd", "--state"])),
        *port_checks(listeners(ss_text) if code == 0 else None, env, low, ufw_allowed(ufw[1]) if ufw[0] == 0 else None,
                     listener_procs(ss_text) if code == 0 else None),
        *mode_checks(root, data),
        *update_checks(Path("/var/run/reboot-required"), _run(["/usr/lib/update-notifier/apt-check"], timeout=30),
                       auto.read_text(encoding="utf-8", errors="replace") if auto.exists() else ("" if shutil.which("apt-get") else None)),
        fail2ban_check(_run(["systemctl", "is-active", "fail2ban"])),
    ]
    return {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "as_root": os.geteuid() == 0, "checks": checks}


def write_report(root: Path, report: dict[str, Any]) -> Path | None:
    """Save the report where Core reads it (data/generated, mounted as /host-generated). None if not writable."""
    env = _dotenv(root / ".env")
    data = Path(env.get("FORGESRE_DATA") or "./data")
    target = (data if data.is_absolute() else root / data) / "generated" / "security-host.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=1), encoding="utf-8")
        target.chmod(0o644)  # Core (another user in the container) reads it; it holds no secrets
    except OSError:
        return None
    return target
