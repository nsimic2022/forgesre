"""Read-only security check: host parsing (SSH, firewall, ports, file modes, updates), the
platform checks, the Admin-only API and the CLI text. Nothing here touches the real host."""

import json
from pathlib import Path

from app import host_security as hs
from app.cli_ops import format_security, security_summary_line
from tests.test_new_ui import _signed_in


def _by(checks):
    return {c["key"]: c for c in checks}


def test_sshd_first_value_wins_includes_and_match():
    included = {"/etc/ssh/sshd_config.d/*.conf": ["PasswordAuthentication no\nKbdInteractiveAuthentication no\n"]}
    text = "Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication yes\nPermitRootLogin no\nMatch User x\nPermitRootLogin yes\n"
    cfg = hs.sshd_settings(text, lambda name: included.get(name, []))
    assert cfg["passwordauthentication"] == "no" and cfg["permitrootlogin"] == "no"
    checks = _by(hs.ssh_checks(cfg))
    assert checks["ssh_password"]["status"] == "ok" and checks["ssh_root"]["status"] == "ok"
    weak = _by(hs.ssh_checks(hs.sshd_settings("PermitRootLogin yes\n")))
    assert weak["ssh_password"]["status"] == "warn" and "10-forgesre.conf" in weak["ssh_password"]["fix"]
    assert weak["ssh_root"]["status"] == "bad"
    assert hs.ssh_checks(None)[0]["status"] == "unknown"
    assert hs.ssh_checks({"_missing": "yes"})[0]["status"] == "info"


def test_firewall_states():
    assert hs.firewall_check((0, "Status: active\n\n22/tcp ALLOW Anywhere\n8080/tcp ALLOW Anywhere\n"), (1, ""), (127, ""))["status"] == "ok"
    off = hs.firewall_check((0, "Status: inactive\n"), (1, ""), (127, ""))
    assert off["status"] == "warn" and "ufw is installed but off" in off["detail"]
    assert hs.firewall_check((127, ""), (0, "table inet f {\n chain input { type filter hook input priority 0; policy drop; }\n}"), (127, ""))["status"] == "ok"
    assert hs.firewall_check((127, ""), (127, ""), (0, "running\n"))["status"] == "ok"
    assert hs.firewall_check((127, ""), (0, ""), (127, ""))["status"] == "warn"


def test_ports_local_only_exposed_unknown_and_https_bypass():
    ss = "\n".join([
        "tcp LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*",
        "tcp LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*",
        "tcp LISTEN 0 4096 0.0.0.0:5432 0.0.0.0:*",
        "tcp LISTEN 0 4096 [::]:22 [::]:*",
        "tcp LISTEN 0 4096 0.0.0.0:6000 0.0.0.0:*",
        "udp UNCONN 0 0 0.0.0.0:514 0.0.0.0:*",
        "udp UNCONN 0 0 0.0.0.0:45123 0.0.0.0:*",
        "udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*",
        "tcp LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*",
    ])
    rows = hs.listeners(ss)
    assert ("tcp", "::", 22) in rows and ("udp", "127.0.0.53", 53) in rows
    out = _by(hs.port_checks(rows, {"FORGESRE_HTTPS": "on", "FORGESRE_HTTP_PORT": "8080"}))
    assert out["ports_local"]["status"] == "bad" and "5432/tcp Postgres" in out["ports_local"]["detail"]
    assert "9090" not in out["ports_local"]["detail"]  # on 127.0.0.1: fine
    assert out["ports_other"]["status"] == "warn" and "6000/tcp" in out["ports_other"]["detail"] and "45123" not in out["ports_other"]["detail"]
    assert "8080/tcp ForgeSRE UI" in out["ports_public"]["detail"] and "514/udp" in out["ports_public"]["detail"]
    assert out["https_bypass"]["status"] == "warn"
    allowed = _by(hs.port_checks(rows, {"FORGESRE_SECURITY_ALLOW_PORTS": "6000", "FORGESRE_BIND": "127.0.0.1"}))
    # 6000 is allowed now; with FORGESRE_BIND=127.0.0.1 and HTTPS off, 8080 and 443 on the network are unexpected
    assert "6000" not in allowed["ports_other"]["detail"] and "8080/tcp" in allowed["ports_other"]["detail"] and "https_bypass" not in allowed
    assert hs.port_checks(None, {})[0]["status"] == "unknown"


def test_file_modes_and_updates(tmp_path):
    (tmp_path / "secrets").mkdir(mode=0o755)
    (tmp_path / "secrets" / "secrets.env").write_text("X=1")
    (tmp_path / "secrets" / "secrets.env").chmod(0o644)
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    (data / "backups").chmod(0o700)
    bad = hs.mode_checks(tmp_path, data)[0]
    assert bad["status"] == "bad" and "chmod 600" in bad["detail"] and "chmod 700" in bad["detail"]
    (tmp_path / "secrets").chmod(0o700)
    (tmp_path / "secrets" / "secrets.env").chmod(0o600)
    assert hs.mode_checks(tmp_path, data)[0]["status"] == "ok"
    reboot = tmp_path / "reboot-required"
    reboot.write_text("")
    (tmp_path / "reboot-required.pkgs").write_text("linux-image-6.8\n")
    up = _by(hs.update_checks(reboot, (0, "12;3"), 'APT::Periodic::Unattended-Upgrade "0";'))
    assert up["updates"]["status"] == "warn" and "3 security" in up["updates"]["detail"]
    assert up["reboot"]["status"] == "warn" and "linux-image-6.8" in up["reboot"]["detail"]
    assert up["auto_updates"]["status"] == "warn"
    fine = _by(hs.update_checks(tmp_path / "nope", (0, "4;0"), 'APT::Periodic::Unattended-Upgrade "1";'))
    assert fine["updates"]["status"] == "ok" and "reboot" not in fine and fine["auto_updates"]["status"] == "ok"


def test_report_is_written_where_core_reads_it(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"FORGESRE_DATA={tmp_path / 'd'}\n")
    path = hs.write_report(tmp_path, {"checked_at": "2026-01-01T08:00:00+00:00", "as_root": True, "checks": [hs.check("x", "X", "warn", "d")]})
    assert path == tmp_path / "d" / "generated" / "security-host.json"
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(path.parent))
    from app.security_report import host_report

    report = host_report()
    assert report["checks"][0]["key"] == "x" and report["stale"] is True


def test_security_api_is_admin_only_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    monkeypatch.setenv("FORGESRE_HTTPS", "off")
    assert _signed_in("engineer").get("/api/v1/system/security").status_code == 403
    data = _signed_in("admin").get("/api/v1/system/security").json()
    keys = {c["key"]: c for c in data["platform"]}
    assert keys["https"]["status"] == "warn" and "./forgesre https on" in keys["https"]["fix"]
    assert {"webhook_token", "admins", "unused", "data_out"} <= set(keys)
    assert data["host"] is None
    (tmp_path / "security-host.json").write_text(json.dumps({"checked_at": "2099-01-01T00:00:00+00:00", "checks": [hs.check("fw", "Firewall", "bad", "off")]}))
    again = _signed_in("admin").get("/api/v1/system/security").json()
    assert again["host"]["checks"][0]["key"] == "fw" and again["summary"]["bad"] >= 1


def test_cli_text():
    rows = [hs.check("a", "SSH password login", "warn", "SSH accepts passwords.", "Put … in 10-forgesre.conf"), hs.check("b", "Firewall", "ok", "ufw is active.")]
    text = format_security("Host", rows)
    assert "! SSH password login" in text and "Fix: Put" in text and "✓ Firewall" in text
    assert security_summary_line(rows) == "!  1 warning(s): ./forgesre security"
    assert security_summary_line(rows[1:]) == "✓"
    assert security_summary_line(rows, "on the host") == "!  1 warning(s) on the host: ./forgesre security"


def test_collect_runs_without_root_and_changes_nothing(tmp_path):
    root = Path(__file__).resolve().parents[1]
    report = hs.collect(root)
    assert {c["key"] for c in report["checks"]} >= {"files", "fail2ban"}
    assert all(c["status"] in {"ok", "warn", "bad", "info", "unknown"} for c in report["checks"])


def test_ufw_rules_lower_findings_for_blocked_ports():
    ufw = ("Status: active\n\nTo                         Action      From\n--                         ------      ----\n"
           "22                         ALLOW       Anywhere\n514/udp                    ALLOW       10.20.0.0/16\n"
           "8080/tcp                   ALLOW       10.0.0.0/24\n6000:6002/tcp              ALLOW       Anywhere\n"
           "53                         ALLOW OUT   Anywhere\n22 (v6)                    ALLOW       Anywhere (v6)\n")
    allowed = hs.ufw_allowed(ufw)
    assert {(22, ""), (514, "udp"), (8080, "tcp"), (6001, "tcp")} <= allowed and (53, "") not in allowed
    assert hs.ufw_allowed("Status: inactive\n") is None
    rows = hs.listeners("\n".join([
        "tcp LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*", "tcp LISTEN 0 4096 0.0.0.0:3000 0.0.0.0:*",
        "udp UNCONN 0 0 0.0.0.0:514 0.0.0.0:*", "tcp LISTEN 0 4096 0.0.0.0:514 0.0.0.0:*",
        "udp UNCONN 0 0 0.0.0.0:161 0.0.0.0:*", "tcp LISTEN 0 4096 *:9094 *:*", "tcp LISTEN 0 4096 0.0.0.0:5432 0.0.0.0:*",
    ]))
    out = _by(hs.port_checks(rows, {"FORGESRE_HTTPS": "on"}, allowed=allowed))
    assert out["ports_local"]["status"] == "warn" and "the firewall blocks them now" in out["ports_local"]["detail"]
    assert "ports_other" not in out and out["ports_blocked"]["status"] == "info"
    assert "161/udp" in out["ports_blocked"]["detail"] and "9094/tcp" in out["ports_blocked"]["detail"]
    pub = out["ports_public"]["detail"]
    assert "Reachable through the firewall: 514/udp" in pub and "8080/tcp ForgeSRE UI" in pub.split("Blocked")[0]
    assert "Blocked by the firewall (usable on this machine only): 3000/tcp Grafana; 514/tcp" in pub
    assert out["https_bypass"]["status"] == "warn"  # 8080 is let in next to HTTPS
    closed = _by(hs.port_checks(rows, {"FORGESRE_HTTPS": "on"}, allowed={(22, "")}))
    assert "https_bypass" not in closed


def test_alertmanager_cluster_port_is_closed_by_default():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "--cluster.listen-address=${ALERTMANAGER_CLUSTER_LISTEN:-}" in compose
