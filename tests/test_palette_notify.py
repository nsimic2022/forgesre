"""Command palette search, new-critical notifications and the installable (PWA) shell."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.db import SessionLocal
from app.models import Asset, Incident, utcnow
from test_incident_tools import _client

ROOT = Path(__file__).resolve().parents[1]


def _incident(db, title: str, severity: str = "CRITICAL", status: str = "OPEN") -> Incident:
    row = Incident(number=f"INC-PN-{uuid4().hex[:6]}", title=title, severity=severity, status=status,
                   fingerprint=f"pn-{uuid4().hex}", started_at=utcnow())
    db.add(row)
    db.commit()
    return row


def test_search_finds_incidents_and_assets():
    db = SessionLocal()
    inc = _incident(db, "pn-search disk full", status="RESOLVED")
    db.add(Asset(asset_id="pn-db01", hostname="pn-db01", ip="10.77.1.5", type="Linux"))
    db.commit()
    client = _client("viewer")
    assert client.get("/api/v1/search?q=p").json() == {"incidents": [], "assets": []}
    got = client.get("/api/v1/search?q=pn-search").json()
    assert [i["number"] for i in got["incidents"]] == [inc.number]
    assert [a["hostname"] for a in client.get("/api/v1/search?q=10.77.1.5").json()["assets"]] == ["pn-db01"]
    db.delete(inc)
    db.query(Asset).filter_by(asset_id="pn-db01").delete()
    db.commit()
    db.close()


def test_new_critical_starts_from_the_newest_and_skips_suppressed():
    db = SessionLocal()
    client = _client("viewer")
    first = client.get("/api/v1/notify/critical").json()
    assert first["items"] == [] and first["latest"] >= 0
    parent = _incident(db, "pn-parent down")
    child = _incident(db, "pn-child down")
    child.caused_by_id = parent.id
    warn = _incident(db, "pn-warning", severity="WARNING")
    db.commit()
    got = client.get(f"/api/v1/notify/critical?after={first['latest']}").json()
    numbers = [i["number"] for i in got["items"]]
    assert parent.number in numbers and child.number not in numbers and warn.number not in numbers
    assert got["latest"] >= warn.id
    for row in (child, parent, warn):
        db.delete(row)
    db.commit()
    db.close()


def test_pwa_files_are_shipped():
    public = ROOT / "web" / "public"
    manifest = json.loads((public / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["scope"] == "/app/" and manifest["display"] == "standalone"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in manifest["icons"]}
    sw = (public / "sw.js").read_text(encoding="utf-8")
    assert "/api" in sw and "navigate" in sw  # only navigations fall back; API is never cached
    assert (public / "offline.html").is_file() and (public / "icon-192.png").is_file()
    assert 'rel="manifest"' in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
