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


def port_checks(rows: list[tuple[str, str, int]] | None, env: dict[str, str], ephemeral_from: int = 32768) -> list[dict[str, str]]:
    """Compare what listens on the network with what ForgeSRE needs: local-only services open to
    the network are bad; unknown open ports are warnings; random high UDP ports (client sockets)
    are ignored."""
    if rows is None:
        return [check("ports", "Open ports", "unknown", "Could not list listening ports (ss is missing).", "Install iproute2.")]
    public = expected_public(env)
    exposed, unknown, known = [], [], set()
    for proto, addr, port in rows:
        if _loopback(addr) or (addr.startswith("127.0.0.53") or addr == "127.0.0.54"):
            continue
        if port in LOCAL_ONLY:
            exposed.append(f"{port}/{proto} {LOCAL_ONLY[port]}")
        elif port in public:
            known.add(f"{port}/{proto} {public[port]}")
        elif port in SYSTEM_PORTS or (proto == "udp" and port >= ephemeral_from):
            continue
        else:
            unknown.append(f"{port}/{proto} on {addr or '*'}")
    out = []
    if exposed:
        out.append(check("ports_local", "Local-only services", "bad",
                         "Open to the network but meant for this host only: " + "; ".join(sorted(set(exposed))) + ".",
                         "Bind them to 127.0.0.1 again (docker-compose.yml was changed?) and block them in the firewall."))
    else:
        out.append(check("ports_local", "Local-only services", "ok", "Databases, Prometheus, Loki, LLM and docker-ro listen on 127.0.0.1 only."))
    if unknown:
        out.append(check("ports_other", "Other open ports", "warn",
                         "Listening on the network, not used by ForgeSRE: " + "; ".join(sorted(set(unknown))) + ".",
                         "Stop the service or block the port in the firewall. If it is needed, list it in "
                         ".env FORGESRE_SECURITY_ALLOW_PORTS=…"))
    out.append(check("ports_public", "ForgeSRE ports", "info",
                     ("Open to the network: " + "; ".join(sorted(known)) + ".") if known else "No ForgeSRE port is open to the network.",
                     "Allow these only from the management network (firewall rule with 'from')."))
    https = str(env.get("FORGESRE_HTTPS") or "off").lower()
    plain = expected_public(env).get(int(env.get("FORGESRE_HTTP_PORT") or 8080))
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
    code, ss_text = _run(["ss", "-Hlntu"])
    try:
        low = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        low = 32768
    auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    checks = [
        *ssh_checks(sshd),
        firewall_check(_run(["ufw", "status"]), _run(["nft", "list", "chains"]), _run(["firewall-cmd", "--state"])),
        *port_checks(listeners(ss_text) if code == 0 else None, env, low),
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
