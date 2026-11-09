"""Discovery first row: Scan now · LibreNMS · NetBox · Zabbix · GLPI, equal fifths."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed
from app.web import librenms_discovery_ctx

ROOT = Path(__file__).resolve().parents[1]


def _client() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_discovery_five_equal_cubes_order():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    snmp = (ROOT / "frontend" / "templates" / "_discovery_snmp.html").read_text(encoding="utf-8")
    actions = css.split(".discovery-actions {", 1)[1].split("}", 1)[0]
    assert "repeat(5, minmax(0, 1fr))" in actions
    assert (
        html.find("Scan now")
        < html.find("<h2>LibreNMS")
        < html.find("NetBox sync")
        < html.find("<h2>Zabbix")
        < html.find("<h2>GLPI")
    )
    assert 'data-librenms-block' in html
    assert "API token {{ librenms.token_present|upper }}" in html
    assert 'href="#snmp"' in html
    assert "librenms_token_present|upper" not in snmp
    assert "127.0.0.1:8000" not in snmp
    assert "No API token" not in snmp


def test_librenms_discovery_ctx_token_gates_identify(monkeypatch):
    from app.settings import settings

    yaml = dict(settings.yaml or {})
    inventory = dict(yaml.get("inventory") or {})
    lnms = dict(inventory.get("librenms") or {})
    lnms["enabled"] = True
    inventory["librenms"] = lnms
    yaml["inventory"] = inventory
    monkeypatch.setattr(settings, "yaml", yaml)
    monkeypatch.setattr(
        "app.librenms.status",
        lambda: {
            "ok": True,
            "light": "green",
            "label": "Connected",
            "why": "2 device(s) in LibreNMS (SNMP polling off after Identify).",
            "hosts": 2,
        },
    )
    monkeypatch.setattr("app.librenms.token_presence", lambda token: "yes")
    row = librenms_discovery_ctx()
    assert row["enabled"] is True
    assert row["token_present"] == "yes"
    assert row["clickable"] is True
    assert row["light"] == "green"
    assert row["label"] == "Connected"
    assert row["why"] == ""

    monkeypatch.setattr("app.librenms.token_presence", lambda token: "no")
    monkeypatch.setattr(
        "app.librenms.status",
        lambda: {
            "ok": False,
            "light": "yellow",
            "label": "No API token",
            "why": "LibreNMS API token missing.",
            "hosts": 0,
        },
    )
    empty = librenms_discovery_ctx()
    assert empty["token_present"] == "no"
    assert empty["clickable"] is False
    assert empty["label"] == "No API token"
    assert "token" in (empty.get("why") or "").lower()


def test_discovery_page_shows_librenms_token_pill(monkeypatch):
    monkeypatch.setattr(
        "app.web_discovery.librenms_discovery_ctx",
        lambda: {
            "enabled": True,
            "url": "http://127.0.0.1:8000",
            "light": "yellow",
            "label": "No API token",
            "why": "LibreNMS API token missing.",
            "hosts": 0,
            "clickable": False,
            "token_present": "no",
        },
    )
    monkeypatch.setattr(
        "app.librenms.status",
        lambda: {
            "ok": False,
            "light": "yellow",
            "label": "No API token",
            "why": "LibreNMS API token missing.",
            "hosts": 0,
        },
    )
    client = _client()
    page = client.get("/discovery")
    assert page.status_code == 200
    head, _, tail = page.text.partition("<h2>SNMP devices</h2>")
    assert 'data-librenms-block' in head
    assert "API token NO" in head
    assert ">Identify<" in head
    assert "disabled" in head.split("data-librenms-block", 1)[1].split("</section>", 1)[0]
    assert "API token NO" not in tail
    assert "LibreNMS API token missing." not in tail
