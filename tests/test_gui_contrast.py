"""Dark NOC contrast pass: stronger text and status tokens, same theme."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
BASE = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")

DARK = {
    "--text": "#f8fafc",
    "--muted": "#cbd5e1",
    "--ok": "#34d399",
    "--warn": "#fbbf24",
    "--crit": "#fda4af",
    "--heading": "#ffffff",
    "--tip": "#ffffff",
    "--check-edge": "#cbd5e1",
    "--check-accent": "#f8fafc",
    "--ink-on-ok": "#022c22",
    "--ink-on-crit": "#4c0519",
    "--pill-ok-bg": "#064e3b",
    "--pill-ok": "#d1fae5",
    "--pill-warn-bg": "#78350f",
    "--pill-warn": "#fef3c7",
    "--pill-crit-bg": "#881337",
    "--pill-crit": "#ffe4e6",
    "--btn-danger-text": "#4c0519",
    "--line": "#334155",
    "--bg": "#0f172a",
    "--panel": "#1e293b",
}

LIGHT = {
    "--text": "#0f172a",
    "--muted": "#64748b",
    "--ok": "#059669",
    "--warn": "#d97706",
    "--crit": "#e11d48",
    "--bg": "#f9fafb",
    "--panel": "#ffffff",
    "--btn-danger-text": "#ffffff",
}


def _rel_lum(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(channel: float) -> float:
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    hi, lo = sorted((_rel_lum(foreground), _rel_lum(background)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _block(start: str) -> str:
    return CSS.split(start, 1)[1].split("}", 1)[0]


def test_cache_bust_is_v09_s_for_css_only():
    assert "app.css?v=v09-39" in BASE
    assert "app.js?v=v09-E" in BASE
    assert "app.css?v=v09-W" not in BASE


def test_light_palette_stays_and_dark_tokens_are_stronger():
    light = _block('html[data-theme="light"],')
    dark = _block('html[data-theme="dark"] {')
    system = _block('  html[data-theme="system"] {')
    for name, value in LIGHT.items():
        assert f"{name}: {value};" in light
    assert "--heading:" not in light
    for name, value in DARK.items():
        assert f"{name}: {value};" in dark
        assert f"{name}: {value};" in system
    panel = DARK["--panel"]
    assert _contrast(DARK["--text"], panel) >= 12
    assert _contrast(DARK["--heading"], panel) > _contrast(DARK["--text"], panel)
    assert _contrast(DARK["--muted"], panel) >= 7
    assert _rel_lum(DARK["--muted"]) < _rel_lum(DARK["--text"])
    assert _contrast(DARK["--tip"], panel) > _contrast(DARK["--muted"], panel)
    assert _contrast(DARK["--ok"], panel) >= 7
    assert _contrast(DARK["--warn"], panel) >= 7
    assert _contrast(DARK["--crit"], panel) >= 5.5
    assert _contrast(DARK["--ink-on-ok"], DARK["--ok"]) >= 7
    assert _contrast(DARK["--ink-on-crit"], DARK["--crit"]) >= 4.5
    assert _contrast(DARK["--btn-danger-text"], DARK["--crit"]) >= 4.5
    assert _contrast(DARK["--pill-ok"], DARK["--pill-ok-bg"]) >= 7
    assert _contrast(DARK["--pill-warn"], DARK["--pill-warn-bg"]) >= 7
    assert _contrast(DARK["--pill-crit"], DARK["--pill-crit-bg"]) >= 7
    assert _contrast(DARK["--ok"], panel) > _contrast("#3d9a6a", panel)
    assert _contrast(DARK["--crit"], panel) > _contrast("#d05a4d", panel)


def test_status_ack_clock_charts_checkbox_and_cube_edges():
    assert "color: var(--ok)" in _block(".dash-recent .status-ack {")
    assert "color: var(--ok)" in _block(".list-scroll .status-ack {")
    collapse = _block(".nav-collapse {")
    assert "position: fixed" in collapse
    assert "color: var(--text)" in _block(".appliance-clock {")
    assert "background: #12151c" in _block(".dash-chart-plot {")
    assert "stroke-width: 2.5" in CSS
    assert "rgba(247, 249, 252, 0.32)" in CSS
    assert "stroke-width: 1.25" in CSS.split(".dash-chart-grid {", 1)[1].split("}", 1)[0]
    check = CSS.split('html[data-theme="dark"] input[type="checkbox"],', 1)[1].split("}", 1)[0]
    assert "var(--check-edge, transparent)" in check
    assert "var(--check-accent, auto)" in check
    assert "width:" not in check
    assert "width: 1rem" in CSS
    assert "min-height: 2.5rem" in CSS
    tip = CSS.split('html[data-theme="dark"] .info-tip,', 1)[1].split("}", 1)[0]
    assert "var(--tip, var(--muted))" in tip
    assert "-webkit-text-stroke: 0.4px var(--tip, transparent)" in tip
    assert "border: 1.5px solid var(--warn)" in _block(".reach-sq {")
    assert "border-color: var(--ok)" in CSS.split(".health-grid .stat.ok {", 1)[1].split("}", 1)[0]
    assert "border-width: 2px" in CSS.split(".stat-row-big .stat.ok {", 1)[1].split("}", 1)[0]
    assert "border-left-width: 8px" in CSS.split(".stat-row-big .stat.ok {", 1)[1].split("}", 1)[0]
    assert "var(--ink-on-ok, #fff)" in CSS
    assert "var(--ink-on-crit, #fff)" in CSS
    assert ".stack-cube.ok { background: var(--ok)" in CSS
    assert ".nav-collapse {" in CSS
