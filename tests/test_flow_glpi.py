"""GLPI flow against the mocked GLPI REST API, and the GLPI exchange log (no tokens kept)."""

from __future__ import annotations

import json

import httpx
import pytest

from app import glpi as glpi_mod
from app.db import Base, SessionLocal, engine
from app.models import IntegrationExchange
from tests.test_glpi import APP_TOKEN, USER_TOKEN, FakeGlpi, _HttpxShim, _reset, _set_glpi


class Fake(FakeGlpi):
    def __init__(self) -> None:
        super().__init__()
        self.rights = 255
        self.refuse = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        leaf = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if leaf == "initSession" and self.refuse:
            return httpx.Response(401, json=["ERROR_GLPI_LOGIN_USER_TOKEN", "parameter user_token seems invalid"])
        if leaf == "getFullSession":
            return httpx.Response(200, json={"session": {"glpiactiveprofile": {"name": "Observer", "computer": self.rights, "networking": 0, "ticket": 1}}})
        if "/Ticket" in request.url.path:
            return httpx.Response(403, json=["ERROR_RIGHT_MISSING", "You don't have permission"])
        return super().handler(request)


@pytest.fixture
def flow(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(IntegrationExchange).filter_by(integration="glpi").delete()
    db.commit()
    fake = Fake()
    fake.computers = [{"id": 1, "name": "db-01"}]
    monkeypatch.setattr(glpi_mod, "httpx", _HttpxShim(httpx.MockTransport(fake.handler)))
    monkeypatch.setattr("app.flow_glpi.network_steps", lambda url, **k: ([{"id": "dns", "label": "DNS", "sub": "glpi.test", "status": "ok", "detail": "", "fix": ""}], True))
    _set_glpi(monkeypatch)
    _reset()
    yield db, fake
    _reset()
    db.close()


def _steps(result, lane):
    return {s["id"]: s for s in next(x for x in result["lanes"] if x["id"] == lane)["steps"]}


def test_glpi_flow_blocks_and_rights(flow):
    from app.flow_glpi import check

    db, fake = flow
    out = _steps(check(db), "out")
    assert out["cfg"]["status"] == "ok" and out["session"]["status"] == "ok"
    assert out["profile"]["status"] == "bad" and "cannot read NetworkEquipment" in out["profile"]["detail"]
    assert out["items"]["status"] == "ok" and "Readable: Computer" in out["items"]["detail"]
    assert fake.calls[-1] == "killSession"
    inside = _steps(check(db), "in")
    assert {"backoff", "sync", "assets"} <= set(inside)


def test_refused_token_names_the_fix_and_is_logged_without_tokens(flow):
    from app.flow_glpi import check

    db, fake = flow
    fake.refuse = True
    out = _steps(check(db), "out")
    assert out["session"]["status"] == "bad" and "set-secret GLPI_USER_TOKEN" in out["session"]["fix"]
    assert "profile" not in out
    rows = db.query(IntegrationExchange).filter_by(integration="glpi").order_by(IntegrationExchange.id).all()
    assert rows and rows[-1].kind == "initSession" and rows[-1].status == "refused" and rows[-1].http_status == 401
    assert rows[-1].peer == "glpi.test"
    dumped = json.dumps([[r.summary, r.endpoint, r.peer, r.detail] for r in rows])
    assert APP_TOKEN not in dumped and USER_TOKEN not in dumped and "sess-test" not in dumped


def test_missing_settings_stop_at_the_first_block(monkeypatch):
    from app.flow_glpi import check

    _set_glpi(monkeypatch, url="", app="", user="")
    db = SessionLocal()
    try:
        out = _steps(check(db), "out")
    finally:
        db.close()
    assert list(out) == ["cfg"] and out["cfg"]["status"] == "bad" and "GLPI_URL" in out["cfg"]["detail"]


def test_ticket_lane_shows_the_missing_create_right_and_failed_calls(flow, monkeypatch):
    from app import tickets
    from app.flow_glpi import check

    db, fake = flow
    shim = _HttpxShim(httpx.MockTransport(fake.handler))
    shim.request = lambda method, url, **k: httpx.Client(transport=shim._transport).request(method, url, **{x: v for x, v in k.items() if x != "timeout"})
    monkeypatch.setattr(tickets, "httpx", shim)
    assert _steps(check(db), "tickets")["tickets"]["status"] == "off"
    monkeypatch.setattr(tickets, "glpi_ready", lambda: "")
    with pytest.raises(RuntimeError):
        tickets._glpi_call("GET", "Ticket/77")
    lane = _steps(check(db), "tickets")
    assert lane["ticket_right"]["status"] == "bad"
    assert lane["ticket_calls"]["status"] == "bad" and "HTTP 403" in lane["ticket_calls"]["detail"]
