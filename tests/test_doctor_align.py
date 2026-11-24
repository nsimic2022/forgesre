"""./forgesre doctor prints one aligned column of marks, with short labels."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from app.stack import COMPONENT_LABELS, LABEL_WIDTH

ROOT = Path(__file__).resolve().parents[1]


def test_labels_fit_the_column():
    too_long = {k: v for k, v in COMPONENT_LABELS.items() if len(v) > LABEL_WIDTH}
    assert not too_long


def test_doctor_marks_line_up(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "doctor.sh", tmp_path / "scripts" / "doctor.sh")
    shutil.copy(ROOT / "scripts" / "dotenv_load.sh", tmp_path / "scripts" / "dotenv_load.sh")
    payload = {"overall": "HEALTHY", "components": {
        "postgres": {"status": "ok", "label": "postgres"},
        "embed": {"status": "ok", "label": COMPONENT_LABELS["embed"]},
        "https": {"status": "disabled", "label": COMPONENT_LABELS["https"]},
        "offbox": {"status": "warn", "label": COMPONENT_LABELS["offbox"], "why": "no copy yet"},
        "netbox": {"status": "error", "label": "NetBox", "why": "down", "fix": "./forgesre restart netbox"},
    }}
    body = tmp_path / "doctor.json"
    body.write_text(json.dumps(payload))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # fake curl: health answers, doctor JSON goes where -o / stdout redirect wants it
    (bin_dir / "curl").write_text(f'#!/bin/sh\ncase "$*" in *system/doctor*) cat {body};; *sd/snmp*) echo "[]";; esac\nexit 0\n')
    (bin_dir / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    done = subprocess.run(["bash", str(tmp_path / "scripts" / "doctor.sh")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    rows = [line for line in done.stdout.splitlines() if line.startswith("  ") and any(m in line for m in "✓–!✗")]
    assert len(rows) == 6, done.stdout + done.stderr
    columns = {line.index(next(m for m in "✓–!✗" if m in line)) for line in rows}
    assert len(columns) == 1, done.stdout
    text = done.stdout
    assert "–  off" in text and "!  no copy yet" in text and "✗  down" in text and "Fix:  ./forgesre restart netbox" in text
