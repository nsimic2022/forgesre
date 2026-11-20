from pathlib import Path
from types import SimpleNamespace


from app.security import can, distinct_who_name, role_label

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


