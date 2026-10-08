"""./forgesre verify without ./forgesre login: the temp admin cookie must outlive the type save."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.cli_ops as cli_ops


def _fake_appliance(monkeypatch, tmp_path: Path) -> dict:
    """Admin creds in secrets.env, no saved CLI session; requests without the cookie get 401 (curl -f)."""
    state: dict = {"saved": [], "jar": None, "targets": 0}
    monkeypatch.setattr(cli_ops, "SESSION_PATH", tmp_path / "missing-cli.session")
    monkeypatch.setattr(
        cli_ops,
        "_secrets",
        lambda: {"FORGESRE_ADMIN_EMAIL": "admin@forgesre.local", "FORGESRE_ADMIN_PASSWORD": "pw"},
    )

    def _login(port, jar, email, password):
        state["jar"] = Path(jar)
        Path(jar).write_text("forgesre_session=abc\n", encoding="utf-8")

    def _authed(jar: Path) -> None:
        if not Path(jar).exists() or "forgesre_session" not in Path(jar).read_text(encoding="utf-8"):
            raise subprocess.CalledProcessError(22, ["curl", "-f"], output=b"", stderr=b"401")

    def get_json(port, jar, path):
        _authed(jar)
        if path == "/api/v1/assets":
            return [{"asset_id": "cli-v-01", "hostname": "cli-v-01", "ip": "10.9.9.9", "type": "Auto (detect)"}]
        return {"assets": {}, "ai_enabled": False, "prometheus_url": "http://127.0.0.1:9", "alertmanager": {"ok": True}}

    def post_json(port, jar, path, payload):
        _authed(jar)
        state["saved"].append((path, dict(payload)))
        return {"type": payload.get("type"), "scrape_address": "10.9.9.9:9100", "monitoring_profile": "linux-standard"}

    def verify_target(item, **kwargs):
        kwargs["targets_fn"]()
        kwargs["targets_fn"]()
        return SimpleNamespace(
            probe=object(),
            lab=False,
            asset_id=item["asset_id"],
            type=item["type"],
            scrape="",
            profile="",
            overall="PASS",
        )

    def prom_targets(url):
        state["targets"] += 1
        return {"targets": []}

    monkeypatch.setattr(cli_ops, "_login", _login)
    monkeypatch.setattr(cli_ops, "_me", lambda port, jar: {"email": "admin@forgesre.local", "role": "super_admin"})
    monkeypatch.setattr(cli_ops, "get_json", get_json)
    monkeypatch.setattr(cli_ops, "post_json", post_json)
    monkeypatch.setattr(cli_ops, "verify_target", verify_target)
    monkeypatch.setattr(cli_ops, "urllib_prom_targets", prom_targets)
    monkeypatch.setattr(cli_ops, "classification_patch", lambda merged, probe: {"type": "Linux Server"})
    monkeypatch.setattr(cli_ops, "format_verify_report", lambda results, **_k: "")
    return state


def test_verify_without_login_saves_detected_type_then_drops_temp_cookie(monkeypatch, tmp_path):
    state = _fake_appliance(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        cli_ops.cmd_verify("8080", [])
    assert exit_info.value.code == 0
    assert state["saved"] == [("/api/v1/assets/cli-v-01", {"type": "Linux Server"})]
    assert state["jar"] is not None and not state["jar"].exists()
    assert state["targets"] == 1


def test_verify_drops_temp_cookie_when_listing_fails(monkeypatch, tmp_path):
    state = _fake_appliance(monkeypatch, tmp_path)

    def broken(port, jar, path):
        raise subprocess.CalledProcessError(7, ["curl"])

    monkeypatch.setattr(cli_ops, "get_json", broken)
    with pytest.raises(SystemExit, match="could not list assets"):
        cli_ops.cmd_verify("8080", [])
    assert state["jar"] is not None and not state["jar"].exists()
    assert state["saved"] == []
