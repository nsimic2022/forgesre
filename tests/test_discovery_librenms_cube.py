"""Discovery first row: wider Scan now · four equal LibreNMS / NetBox / Zabbix / GLPI."""

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


def test_discovery_scan_now_wider_four_equal_sync_cubes():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    snmp = (ROOT / "frontend" / "templates" / "_discovery_snmp.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    actions = css.split(".discovery-actions {", 1)[1].split("}", 1)[0]
    assert "minmax(0, 1.5fr) repeat(4, minmax(0, 1fr))" in actions
    assert "repeat(5, minmax(0, 1fr))" not in actions
    assert "discovery-scan-card { grid-column: 1 / -1; }" in css
    assert 'class="card discovery-scan-card"' in html
    cube = html.split("data-librenms-block", 1)[1].split("</section>", 1)[0]
    assert "Sync hosts" not in cube
    assert 'href="#snmp"' in cube
    assert 'hx-boost="false"' in cube
    assert "librenms-meta" in cube
    assert "librenms-webhook" in cube
    assert "librenms.webhook_url" in cube
    assert (
        html.find("Scan now")
        < html.find("<h2>LibreNMS")
        < html.find("NetBox sync")
        < html.find("<h2>Zabbix")
        < html.find("<h2>GLPI")
    )
    assert "API token {{ librenms.token_present|upper }}" in html
    assert "librenms_token_present|upper" not in snmp
    assert "127.0.0.1:8000" not in snmp
    assert "No API token" not in snmp
    assert 'id="snmp-identify"' in snmp
    assert "scrollDiscoverySnmp" in js
    assert 'getElementById("snmp-identify")' in js
    assert 'data-snmp-focus="identify"' in html


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


def test_discovery_page_shows_librenms_why_and_webhook(monkeypatch):
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
    cube = head.split("data-librenms-block", 1)[1].split("</section>", 1)[0]
    assert "API token NO" in cube
    assert "LibreNMS API token missing." in cube
    assert ">Identify<" in cube
    assert "disabled" in cube
    assert "/api/v1/webhooks/librenms" in cube
    assert "Sync hosts" not in cube
    assert "API token NO" not in tail
    assert "LibreNMS API token missing." not in tail
    assert html_snmp_below_add(page.text)


def html_snmp_below_add(html: str) -> bool:
    return html.index(">Add asset<") < html.index('id="snmp"') < html.index(">SNMP devices<")
