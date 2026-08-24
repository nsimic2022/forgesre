from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.security import can, role_label
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def test_role_split_analyst_writes_play_engineer_sees_evidence():
    analyst = SimpleNamespace(role="analyst")
    engineer = SimpleNamespace(role="engineer")
    admin = SimpleNamespace(role="admin")
    super_admin = SimpleNamespace(role="super_admin")
    viewer = SimpleNamespace(role="viewer")

    assert can(analyst, "write_play")
    assert can(analyst, "write_incidents")
    assert can(analyst, "read_ai")
    assert not can(analyst, "read_evidence")
    assert can(analyst, "write_assets")
    assert not can(analyst, "admin")

    assert can(engineer, "read_evidence")
    assert can(engineer, "investigate")
    assert can(engineer, "write_assets")
    assert not can(engineer, "write_play")
    assert not can(engineer, "admin")

    assert can(admin, "admin")
    assert can(admin, "write_assets")
    assert can(super_admin, "super_admin")
    assert not can(viewer, "ack_incidents")
    assert "Analyst" in role_label("analyst")
    assert "Engineer" in role_label("engineer")


def test_role_label_is_one_human_phrase():
    expected = {
        "super_admin": "Super admin",
        "admin": "System admin",
        "system_admin": "System admin",
        "analyst": "Analyst",
        "engineer": "Engineer",
        "viewer": "Viewer",
    }
    for role, label in expected.items():
        assert role_label(role) == label
        assert "_" not in role_label(role)
        assert role_label(role).count(label) == 1
    assert role_label("super_admin") != "super_admin"
    assert "super_admin" not in role_label("super_admin")


def test_nav_foot_prints_role_once_and_theme_switcher():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    login = client.get("/login")
    assert login.status_code == 200
    assert login.text.count("data-theme-toggle") == 1
    assert 'data-theme="light"' in login.text
    assert "forgesre-theme" in login.text
    assert "prefers-color-scheme" not in login.text
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200
    assert home.text.count("data-theme-toggle") == 1
    assert home.text.count('class="who-role"') == 1
    assert ">Super admin<" in home.text
    assert home.text.count(">Super admin<") == 1
    assert "super_admin" not in home.text
    assert "Super admin (system)" not in home.text
    logout = home.text.split("nav-foot-actions", 1)[1]
    assert 'class="secondary"' in logout
    assert ">Logout<" in logout
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'html[data-theme="light"]' in css
    assert 'html[data-theme="dark"]' in css
    assert 'html[data-theme="high-contrast"]' in css
    assert "prefers-color-scheme" not in css
    assert "forgesre-theme" in js
    assert "matchMedia" not in js
    db.close()
