from types import SimpleNamespace

from app.security import can, role_label


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
