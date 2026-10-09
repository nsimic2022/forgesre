"""Host chart axes, hover tooltip, and Y scale. Series stay real samples."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "static" / "app.js"
CSS = ROOT / "frontend" / "static" / "app.css"

_HELPERS = r"""
const SRC = process.argv[1];
eval(SRC);
const covers = (kind, values) => {
  const scale = chartYScale(kind, values);
  const nums = values.map(Number);
  const min = Math.min.apply(null, nums);
  const max = Math.max.apply(null, nums);
  return {
    mode: scale.mode,
    lo: scale.lo,
    hi: scale.hi,
    covers: scale.lo <= min + 1e-9 && scale.hi >= max - 1e-9,
  };
};
const noon = new Date(2026, 9, 9, 12, 0, 0).getTime() / 1000;
const later = new Date(2026, 9, 10, 15, 0, 0).getTime() / 1000;
console.log(JSON.stringify({
  tiny: covers("percent", [0.05, 0.1, 0.2, 0.08]),
  swing: covers("percent", [10, 14, 11]),
  drop: covers("percent", [40, 40, 0]),
  toward: covers("percent", [20, 20, 100]),
  over: covers("percent", [80, 110, 0]),
  smallDrop: covers("percent", [12, 12, 0]),
  highBand: covers("percent", [92, 94, 93]),
  up: chartYScale("up", [1, 1, 0]),
  ticks: chartTicks(0, 100),
  clocks: chartClockTicks(noon, noon + 3600),
  multi: chartClockTicks(noon, later),
  tip: chartTipLines("hapfusion2-prod", noon, "CPU", "percent", 0.1, "%"),
}));
"""


def _helper_src() -> str:
    src = JS.read_text(encoding="utf-8")
    body = src.split("\nfunction chartExtent(", 1)[1].split("\nfunction graphPane(", 1)[0]
    return "function chartExtent(" + body


def _helpers() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "-e", _HELPERS, "--", _helper_src()],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_y_scale_autos_a_tiny_band_and_keeps_full_scale_for_a_drop():
    out = _helpers()
    tiny = out["tiny"]
    assert tiny["covers"] is True
    assert tiny["mode"] == "auto"
    assert tiny["hi"] - tiny["lo"] < 5
    assert tiny["hi"] < 1

    swing = out["swing"]
    assert swing["covers"] is True
    assert swing["mode"] == "auto"
    assert swing["hi"] < 30
    assert swing["lo"] > 0

    drop = out["drop"]
    assert drop["covers"] is True
    assert drop["mode"] == "full"
    assert drop["lo"] <= 0
    assert drop["hi"] >= 100

    toward = out["toward"]
    assert toward["covers"] is True
    assert toward["mode"] == "full"
    assert toward["lo"] <= 0 and toward["hi"] >= 100

    over = out["over"]
    assert over["covers"] is True
    assert over["mode"] == "full"
    assert over["lo"] <= 0
    assert over["hi"] >= 110

    small = out["smallDrop"]
    assert small["covers"] is True
    assert small["mode"] == "full"
    assert small["lo"] <= 0
    assert 12 <= small["hi"] < 50

    high = out["highBand"]
    assert high["covers"] is True
    assert high["mode"] == "full"
    assert high["lo"] <= 0 and high["hi"] >= 100

    assert out["up"]["lo"] == 0 and out["up"]["hi"] == 1 and out["up"]["mode"] == "full"
    assert out["ticks"] == [0, 25, 50, 75, 100]


def test_clock_labels_and_tooltip_lines():
    out = _helpers()
    clocks = out["clocks"]
    assert [tick["time"] for tick in clocks] == ["12:00", "12:20", "12:40", "13:00"]
    assert all(tick["date"] == "" for tick in clocks)
    multi = out["multi"]
    assert multi[0]["time"] == "12:00" and multi[0]["date"] == "09.10"
    assert multi[-1]["time"] == "15:00"
    assert any(tick["date"] == "10.10" for tick in multi)
    tip = out["tip"]
    assert tip["host"] == "hapfusion2-prod"
    assert tip["time"] == "09.10.2026 12:00:00"
    assert tip["value"] == "CPU 0.1%"


def test_chart_markup_has_axes_grid_and_hover_tooltip():
    from test_dashboard_graphs import _run_js

    start = datetime(2026, 10, 9, 12, 51, tzinfo=timezone.utc).timestamp()
    end = start + 3600
    rows = [{"number": "INC-1", "asset": "hap-01", "host": "hapfusion2-prod", "active": "true"}]
    panel = {
        "hostname": "hapfusion2-prod",
        "collecting": True,
        "collecting_line": "Prometheus sees this target (up=1).",
        "source": "prometheus",
        "window": {"start": start, "end": end, "marker": start + 1800, "label": "12:51–13:51"},
        "tiles": [
            {
                "key": "cpu_percent",
                "name": "CPU",
                "kind": "percent",
                "tone": "ok",
                "value": 0.1,
                "display": "0.1%",
                "series": [0.05, 0.1, 0.2, 0.08],
                "times": [start, start + 1200, start + 2400, end],
            }
        ],
    }
    out = _run_js(rows, panel)
    axis = out["initial"]["axes"][0]
    assert axis["scale"] == "auto"
    assert axis["ymax"] - axis["ymin"] < 5
    assert axis["unit"] == "%"
    assert "100" not in axis["y"]
    assert axis["y"]
    assert len(axis["x"]) == 4
    assert all(re.fullmatch(r"\d{2}:\d{2}( \d{2}\.\d{2})?", label) for label in axis["x"])
    assert axis["x"][0].startswith(datetime.fromtimestamp(start).strftime("%H:%M"))
    assert axis["grids"] >= 4
    assert axis["tip"] is True
    ys = [float(pair.split(",")[1]) for pair in out["initial"]["points"][0].split() if "," in pair]
    assert max(ys) - min(ys) > 10
    tip = out["initial"]["hoverTip"]
    assert tip["hidden"] is False
    assert tip["host"] == "hapfusion2-prod"
    assert tip["value"] == "CPU 0.05%"
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}", tip["time"])
    assert out["initial"]["markerX"] == [pytest.approx(100.0, abs=0.6)]

    drop = {
        "hostname": "hapfusion2-prod",
        "collecting": True,
        "source": "prometheus",
        "window": {"start": 0, "end": 100, "marker": 50, "label": "11:15–13:15"},
        "tiles": [
            {
                "key": "cpu_percent",
                "name": "CPU",
                "kind": "percent",
                "tone": "crit",
                "value": 0,
                "display": "0%",
                "threshold": 95,
                "alarm_enabled": True,
                "series": [40, 40, 0],
                "times": [0, 40, 70],
            }
        ],
    }
    crashed = _run_js(rows, drop)
    full = crashed["initial"]["axes"][0]
    assert full["scale"] == "full"
    assert full["ymin"] <= 0 and full["ymax"] >= 100
    assert full["y"] == ["100", "75", "50", "25", "0"]
    assert full["unit"] == "%"
    assert len(full["x"]) == 4
    assert crashed["initial"]["hoverTip"]["value"] == "CPU 40%"
    assert crashed["initial"]["hoverTip"]["host"] == "hapfusion2-prod"
    ys = [float(pair.split(",")[1]) for pair in crashed["initial"]["points"][0].split() if "," in pair]
    assert ys[-1] - ys[0] > 15
    assert crashed["initial"]["markerX"] == [100.0]


def test_empty_copy_and_no_chart_chrome():
    js = JS.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'data.collecting === false ? "Not scraped." : "No samples yet."' in js
    assert "No host to chart" in js
    assert "Math.sin" not in js
    assert "Chart Options" not in js and "Real-time" not in js
    assert "dash-chart-tip-host" in js and "dash-chart-grid" in js and "dash-chart-marker" in js
    assert "#12151c" in css and ".dash-chart-grid" in css and ".dash-chart-tip" in css
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-3" in base and "app.js?v=v09-3" in base
    for name in ("dashboard.html", "incident_detail.html", "asset_detail.html"):
        text = (ROOT / "frontend" / "templates" / name).read_text(encoding="utf-8")
        assert "Chart Options" not in text and "Real-time" not in text
        assert "No samples yet" in text
    dash = (ROOT / "frontend" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    asset_tpl = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    assert "Not scraped" in dash and "Not scraped" in asset_tpl
    incident = (ROOT / "frontend" / "templates" / "incident_detail.html").read_text(encoding="utf-8")
    asset = (ROOT / "frontend" / "templates" / "asset_detail.html").read_text(encoding="utf-8")
    assert 'data-host="{{ incident.asset.hostname }}"' in incident
    assert 'data-host="{{ asset.hostname }}"' in asset
