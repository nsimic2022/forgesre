"""Postgres rejects BOOLEAN DEFAULT 0; discovery flags must use FALSE."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_discovery_boolean_alters_use_false_not_integer():
    src = (ROOT / "backend" / "app" / "migrate.py").read_text(encoding="utf-8")
    for col in ("snmp_ok", "node_exporter", "windows_exporter"):
        assert f"ADD COLUMN {col} BOOLEAN DEFAULT FALSE" in src
        assert f"ADD COLUMN {col} BOOLEAN DEFAULT 0" not in src
    # Postgres path already used TRUE; keep sqlite DEFAULT 1 out of this check.
    assert "snmp_ok BOOLEAN DEFAULT 0" not in src
    assert "node_exporter BOOLEAN DEFAULT 0" not in src
    assert "windows_exporter BOOLEAN DEFAULT 0" not in src
