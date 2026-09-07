"""Session handoff doc for the next contributor. No live Docker stack required."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_continuation_handoff_exists_and_points_at_test_and_llm():
    path = ROOT / "docs" / "continuation.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "./forgesre test" in text
    assert "./forgesre ping" in text
    assert "./forgesre verify" in text
    assert "docs/llm.md" in text
    assert "no host logs shipped" in text.lower() or "No host logs shipped" in text
    assert "architecture proposal" in text.lower() or "not the V0.7 appliance runtime" in text
    assert "one worker thread" in text.lower()
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "V0.7" in contributing
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "memory 90%" in handbook
    assert "Grafana is not the alarm path" in handbook
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "Not the V0.7 appliance runtime" in arch
    assert "Do **not** implement the Go / Kubernetes" in arch
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "iputils-ping" in dockerfile
    assert "celery" not in dockerfile.lower()
    install = (ROOT / "docs" / "install-config.md").read_text(encoding="utf-8")
    assert "6379" in install
    assert "TCP/25" in install or "port 25" in install
    assert "993" in install
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "continuation.md" in index
    operators, _, developers = index.partition("For developers")
    assert "continuation.md" in developers
    assert "continuation.md" not in operators
