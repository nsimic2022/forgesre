from app.cli_view import RED, GREEN, YELLOW, format_board, format_detail, format_history_rows, lane_for


def _item(**kwargs):
    row = {
        "number": "INC-000001",
        "status": "OPEN",
        "severity": "WARNING",
        "title": "High CPU",
        "asset": {"hostname": "db-01"},
        "ack_by": "",
        "resolved_by": "",
    }
    row.update(kwargs)
    return row


def test_lanes_use_red_yellow_green_meaning():
    assert lane_for(_item(status="OPEN", severity="CRITICAL")) == "critical"
    assert lane_for(_item(status="ESCALATED", severity="WARNING")) == "critical"
    assert lane_for(_item(status="INVESTIGATING", severity="WARNING")) == "progress"
    assert lane_for(_item(status="OPEN", severity="WARNING")) == "progress"
    assert lane_for(_item(status="CLOSED", severity="CRITICAL")) == "done"
    assert lane_for(_item(status="RESOLVED", severity="WARNING")) == "done"


def test_board_groups_and_stays_short_without_color():
    rows = [
        _item(number="INC-000010", status="OPEN", severity="CRITICAL", title="disk full"),
        _item(number="INC-000011", status="INVESTIGATING", title="cpu"),
        _item(number="INC-000012", status="CLOSED", title="old"),
    ]
    text = format_board(rows, color=False, who="eng@dc.local (engineer)")
    assert "CRITICAL / OPEN" in text
    assert "IN PROGRESS" in text
    assert "DONE" in text
    assert "INC-000010" in text
    assert "INC-000012" in text
    assert "eng@dc.local (engineer)" in text
    assert "incidents INC-0134_16.08.2026_09:13" in text or "incidents <TAB>" in text
    assert "\033[" not in text


def test_board_colors_when_enabled():
    rows = [
        _item(number="INC-000010", status="OPEN", severity="CRITICAL"),
        _item(number="INC-000011", status="INVESTIGATING"),
        _item(number="INC-000012", status="CLOSED"),
    ]
    text = format_board(rows, color=True)
    assert RED in text
    assert YELLOW in text
    assert GREEN in text


def test_detail_and_history_rows():
    detail = format_detail(
        {
            **_item(number="INC-000099", status="CLOSED"),
            "notifications": [{"created_at": "t", "status": "generated", "target": "a@b", "subject": "s", "body": "hi"}],
            "audit": [{"at": "t", "actor": "eng@dc.local", "action": "incident.status", "data": {"status": "CLOSED"}}],
            "notes": [{"at": "t", "actor": "eng@dc.local", "body": "cleaned WAL"}],
        },
        color=False,
    )
    assert "INC-000099" in detail
    assert "cleaned WAL" in detail
    assert "generated" in detail
    hist = format_history_rows(
        [_item(number="INC-000003", status="CLOSED", ack_by="ana@dc.local")],
        days=90,
        total=1,
        color=False,
    )
    assert "History last 90" in hist
    assert "INC-000003" in hist
    assert "ana@dc.local" in hist
