from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import can, distinct_who_name, hash_password, role_label
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def test_four_roles_form_one_ladder():
    viewer = SimpleNamespace(role="viewer")
    analyst = SimpleNamespace(role="analyst")
    engineer = SimpleNamespace(role="engineer")
    admin = SimpleNamespace(role="admin")

    assert can(viewer, "read_incidents") and not can(viewer, "ack_incidents")

    # Analyst = NOC operator: works incidents, runs local investigations, edits nothing else.
    for perm in ("ack_incidents", "write_incidents", "read_ai", "read_evidence", "investigate"):
        assert can(analyst, perm)
    for perm in ("write_assets", "write_play", "ai_cloud", "run_checks", "admin"):
        assert not can(analyst, perm)

    # Engineer changes everything operational, for every team, but not settings or users.
    for perm in ("write_assets", "write_play", "ai_cloud", "run_checks", "investigate"):
        assert can(engineer, perm)
    assert not can(engineer, "admin")

    assert can(admin, "admin") and can(admin, "write_play")
    for lower, higher in ((viewer, analyst), (analyst, engineer), (engineer, admin)):
        from app.security import PERMISSIONS

        assert PERMISSIONS[lower.role] < PERMISSIONS[higher.role]


def test_legacy_admin_roles_keep_full_access():
    for legacy in ("super_admin", "system_admin"):
        user = SimpleNamespace(role=legacy)
        assert can(user, "admin")
        assert role_label(legacy) == "Admin"


def test_role_label_is_one_human_phrase():
    expected = {"admin": "Admin", "analyst": "Analyst", "engineer": "Engineer", "viewer": "Viewer"}
    for role, label in expected.items():
        assert role_label(role) == label
    assert role_label("on_call_lead") == "On Call Lead"


def test_distinct_who_name_skips_role_echo():
    assert distinct_who_name("Admin", "admin") == ""
    assert distinct_who_name("  admin  ", "admin") == ""
    assert distinct_who_name("Nataša", "admin") == "Nataša"
    assert distinct_who_name("Ada", "analyst") == "Ada"
    assert distinct_who_name("N", "admin", "Simić") == "N Simić"
    assert distinct_who_name("  Analyst ", "analyst") == ""
    assert distinct_who_name("", "admin") == ""
    assert distinct_who_name(None, "admin") == ""


def _who_block(html: str) -> str:
    return html.split('class="who"', 1)[1].split("nav-foot-actions", 1)[0]


def test_nav_foot_prints_role_once_and_theme_switcher():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    login = client.get("/login")
    assert login.status_code == 200
    assert login.text.count("data-theme-toggle") == 1
    assert 'class="theme-toggle"' in login.text
    assert "secondary theme-toggle" not in login.text
    assert 'data-theme="dark"' in login.text
    assert "forgesre-theme" in login.text
    assert "prefers-color-scheme" not in login.text
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200
    assert home.text.count("data-theme-toggle") == 1
    assert 'class="theme-toggle"' in home.text
    assert "secondary theme-toggle" not in home.text
    who = _who_block(home.text)
    assert home.text.count('class="who-role"') == 1
    assert 'class="who-name"' not in who
    assert ">Admin<" in who
    assert "super_admin" not in home.text
    assert "Super admin" not in home.text
    logout = home.text.split("nav-foot-actions", 1)[1]
    assert 'class="secondary"' in logout
    assert ">Logout<" in logout
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "button.theme-toggle" in css
    assert "min-width: 6.2rem" not in css
    assert 'html[data-theme="light"]' in css
    assert 'html[data-theme="dark"]' in css
    assert 'html[data-theme="system"]' in css
    assert 'html[data-theme="contrast"]' in css
    assert 'html[data-theme="high-contrast"]' not in css
    assert "prefers-color-scheme" in css
    assert "--shell-bg: #ffffff" in css
    assert "--accent: #00e676" in css
    assert "forgesre-theme" in js
    assert '"system"' in js
    assert '"contrast"' in js
    assert "high-contrast" in js
    assert "matchMedia" not in js
    assert "theme-label-system" in base
    assert "theme-label-contrast" in base
    assert "distinct_who_name(user.name, user.role, user.surname)" in base
    assert "class=\"secondary theme-toggle\"" not in base
    db.close()


def test_nav_foot_keeps_distinct_name_above_role():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.add(
        User(
            email="ada@forgesre.local",
            name="Ada",
            surname="Lovelace",
            password_hash=hash_password("testpass"),
            role="analyst",
        )
    )
    db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "ada@forgesre.local", "password": "testpass"}, follow_redirects=False)
    home = client.get("/")
    assert home.status_code == 200
    who = _who_block(home.text)
    assert 'class="who-name"' in who
    assert ">Ada Lovelace<" in who
    assert who.count(">Ada Lovelace<") == 1
    assert ">Analyst<" in who
    assert who.count(">Analyst<") == 1
    db.close()
