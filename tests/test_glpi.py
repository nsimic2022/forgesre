"""Optional GLPI read-only import. Mocked REST API — no live GLPI."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app import api as api_mod
from app import glpi as glpi_mod
from app.db import Base, SessionLocal, engine
from app.exporter_detect import AUTO_ASSET_TYPE
from app.glpi import api_root, list_inventory
from app.glpi_sync import glpi_asset_id, sync_glpi
from app.inventory import asset_in_glpi, asset_source_tokens
from app.main import app
from app.models import Asset, User
from app.security import hash_password
from app.seed import seed
from app.settings import settings

ROOT = Path(__file__).resolve().parents[1]
GLPI_URL = "https://glpi.test/glpi"
APP_TOKEN = "glpi-app-token-test"
USER_TOKEN = "glpi-user-token-test"


class FakeGlpi:
    def __init__(self) -> None:
        self.computers: list[dict] = []
        self.network: list[dict] = []
        self.addresses: list[dict] = []
        self.down = False
        self.calls: list[str] = []
        self.session = "sess-test-1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path.rstrip("/")
        leaf = path.rsplit("/apirest.php/", 1)[-1] if "/apirest.php/" in path else path.rsplit("/", 1)[-1]
        self.calls.append(leaf)
        if leaf == "initSession":
            assert request.headers.get("App-Token") == APP_TOKEN
            assert "user_token" in (request.headers.get("Authorization") or "")
            return httpx.Response(200, json={"session_token": self.session})
        if leaf == "killSession":
            return httpx.Response(200, json={"status": "ok"})
        if leaf == "Computer":
            return httpx.Response(200, json=self.computers)
        if leaf == "NetworkEquipment":
            return httpx.Response(200, json=self.network)
        if leaf == "IPAddress":
            return httpx.Response(200, json=self.addresses)
        return httpx.Response(404, json={"message": f"no {leaf}"})


class _HttpxShim:
    def __init__(self, transport: httpx.MockTransport) -> None:
        self._transport = transport

    def __getattr__(self, name: str):
        return getattr(httpx, name)

    def Client(self, **kwargs):  # noqa: N802
        kwargs.pop("transport", None)
        return httpx.Client(transport=self._transport, **kwargs)


def _set_glpi(monkeypatch, *, url: str = GLPI_URL, app: str = APP_TOKEN, user: str = USER_TOKEN) -> None:
    cls = type(settings)
    monkeypatch.setattr(cls, "glpi_url", property(lambda self: url))
    monkeypatch.setattr(cls, "glpi_app_token", property(lambda self: app))
    monkeypatch.setattr(cls, "glpi_user_token", property(lambda self: user))


def _reset() -> None:
    glpi_mod.reset_state()
    api_mod._doctor_cache.update(at=0.0, payload=None)


@pytest.fixture
def glpi(monkeypatch):
    fake = FakeGlpi()
    monkeypatch.setattr(glpi_mod, "httpx", _HttpxShim(httpx.MockTransport(fake.handler)))
    _set_glpi(monkeypatch)
    _reset()
    yield fake
    _reset()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for row in db.query(Asset).filter(Asset.asset_id.like("glpi-%")).all():
        db.delete(row)
    for row in db.query(Asset).filter(Asset.source == "glpi").all():
        db.delete(row)
    db.commit()
    db.close()


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def test_api_root_appends_apirest():
    assert api_root("https://glpi.example.local/glpi") == "https://glpi.example.local/glpi/apirest.php"
    assert api_root("https://glpi.example.local/apirest.php") == "https://glpi.example.local/apirest.php"


def test_glpi_asset_id_slug():
    assert glpi_asset_id("NOC Switch 01", "NetworkEquipment:4") == "noc-switch-01"
    assert glpi_asset_id("", "Computer:12").startswith("glpi-")


def test_list_inventory_maps_ip_and_refuses_writes(glpi):
    glpi.computers = [{"id": 12, "name": "core-sw", "locations_id": "DC-East", "entities_id": "Acme"}]
    glpi.addresses = [{"name": "10.10.10.8", "mainitemtype": "Computer", "mainitems_id": 12}]
    rows = list_inventory(GLPI_URL, APP_TOKEN, USER_TOKEN, timeout=3, force=True)
    assert rows[0]["glpi_id"] == "Computer:12"
    assert rows[0]["ip"] == "10.10.10.8"
    assert rows[0]["location"] == "DC-East"
    with pytest.raises(glpi_mod.GlpiError, match="read-only"):
        glpi_mod._get("Computer/12", url=GLPI_URL, app_token=APP_TOKEN, session="x", timeout=3)


def test_sync_creates_then_links_without_overwriting(glpi):
    db = _db()
    glpi.computers = [{"id": 12, "name": "edge-fw", "comment": "from glpi"}]
    glpi.addresses = [{"name": "10.9.9.9", "mainitemtype": "Computer", "mainitems_id": 12}]
    first = sync_glpi(db, force=True)
    assert first["created"] == 1
    asset = db.query(Asset).filter_by(glpi_id="Computer:12").one()
    assert asset.source == "glpi"
    assert asset.type == AUTO_ASSET_TYPE
    assert asset.scrape_address == ""
    assert asset.ip == "10.9.9.9"
    assert "Imported from GLPI" in asset.notes
    asset.owner = "keep-me"
    asset.notes = "operator note"
    db.commit()
    glpi.computers[0]["name"] = "renamed"
    second = sync_glpi(db, force=True)
    db.refresh(asset)
    assert second["linked"] == 1
    assert second["created"] == 0
    assert asset.owner == "keep-me"
    assert asset.notes == "operator note"
    db.close()


def test_sync_links_existing_asset_by_ip(glpi):
    db = _db()
    existing = Asset(
        asset_id="manual-edge",
        hostname="edge",
        ip="10.8.8.8",
        type="Linux Server",
        environment="Production",
        source="manual",
        owner="noc",
    )
    db.add(existing)
    db.commit()
    glpi.computers = [{"id": 44, "name": "edge"}]
    glpi.addresses = [{"name": "10.8.8.8", "mainitemtype": "Computer", "mainitems_id": 44}]
    result = sync_glpi(db, force=True)
    db.refresh(existing)
    assert result["linked"] == 1
    assert existing.glpi_id == "Computer:44"
    assert existing.owner == "noc"
    assert existing.source == "manual"
    assert asset_in_glpi(existing)
    assert "glpi" in asset_source_tokens(existing)
    db.delete(existing)
    db.commit()
    db.close()


def test_discovery_page_has_glpi_cube_and_sync_route(glpi):
    client = _login()
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "<h2>GLPI" in page.text
    assert 'action="/discovery/glpi-sync"' in page.text
    assert "Mode:" not in page.text.split("<h1>", 1)[1].split("discovery-actions", 1)[0]
    assert page.text.find("Scan now") < page.text.find("NetBox sync") < page.text.find("<h2>Zabbix") < page.text.find("<h2>GLPI")
    glpi.computers = [{"id": 1, "name": "lab-pc"}]
    posted = client.post("/discovery/glpi-sync", follow_redirects=False)
    assert posted.status_code == 302
    assert "GLPI sync" in unquote(posted.headers.get("location") or "")


def test_api_glpi_sync_and_doctor(glpi):
    glpi.computers = [{"id": 3, "name": "api-pc"}]
    client = _login()
    resp = client.post("/api/v1/discovery/glpi-sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("created") == 1
    doctor = client.get("/api/v1/system/doctor")
    assert doctor.status_code == 200
    glpi_row = doctor.json()["components"]["glpi"]
    assert glpi_row["label"] == "GLPI"
    assert glpi_row["status"] in {"ok", "warn", "disabled"}


def test_docs_and_secrets_name_the_three_keys():
    secrets = (ROOT / "secrets" / "secrets.example.env").read_text(encoding="utf-8")
    for key in ("GLPI_URL=", "GLPI_APP_TOKEN=", "GLPI_USER_TOKEN="):
        assert key in secrets
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    cook = (ROOT / "docs" / "glpi.md").read_text(encoding="utf-8")
    assert "never writes back" in cook.lower() or "never writes to glpi" in cook.lower().replace("*", "")
    assert "GLPI_APP_TOKEN" in cook and "Sync hosts" in cook
    assert "glpi.md" in handbook
    install = (ROOT / "docs" / "install-config.md").read_text(encoding="utf-8")
    assert "GLPI_URL" in install


def test_unconfigured_sync_is_skipped(monkeypatch):
    _set_glpi(monkeypatch, url="", app="", user="")
    db = _db()
    result = sync_glpi(db, force=True)
    assert result.get("skipped") is True
    db.close()


def test_viewer_cannot_sync():
    db = _db()
    db.add(
        User(
            email="view-glpi@dc.local",
            name="View",
            password_hash=hash_password("view-pass"),
            role="viewer",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "view-glpi@dc.local", "password": "view-pass"}, follow_redirects=False)
    denied = client.post("/discovery/glpi-sync", follow_redirects=False)
    assert denied.status_code == 403
    db.query(User).filter_by(email="view-glpi@dc.local").delete()
    db.commit()
    db.close()
