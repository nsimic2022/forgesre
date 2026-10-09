"""Dashboard: Recent incidents left, Prometheus graphs for the selected incident's host right. No Grafana."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_metrics import asset_metric_panel
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset, Incident
from app.seed import DEMO_ASSET, seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "static" / "app.js"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _drop_rows():
    yield
    db = _db()
    db.query(Incident).filter(Incident.fingerprint.like("dashgraph:%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add(db, *, status: str = "OPEN", asset: Asset | None = None, title: str = "") -> Incident:
    row = Incident(
        number=next_incident_number(db),
        title=title or f"Dash graph {uuid4().hex[:6]}",
        severity="WARNING",
        status=status,
        fingerprint=f"dashgraph:{uuid4().hex}",
        started_at=datetime.now(timezone.utc),
        asset_id=asset.id if asset is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _row_attrs(html: str, number: str) -> dict[str, str]:
    match = re.search(r'<tr [^>]*data-dash-incident="' + re.escape(number) + r'"[^>]*>', html)
    assert match is not None, number
    return dict(re.findall(r'(data-[\w-]+)="([^"]*)"', match.group(0)))


def test_dashboard_has_graph_pane_right_of_recent_incidents():
    _db().close()
    html = _client().get("/").text
    wrap = html.index("data-dash-incidents")
    recent = html.index("<h2>Recent incidents</h2>")
    table = html.index("data-dash-incident-table")
    pane = html.index("data-dash-graphs")
    assert wrap < recent < table < pane
    assert "Recent journal" not in html
    aside = html[pane : html.index("</aside>", pane)]
    assert "Host metrics" in aside
    assert "data-dash-graph-list" in aside and "data-dash-graph-empty" in aside
    assert 'class="pager-bar"' in html[recent:pane] or "data-select-page" in html[recent:pane]
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    block = css.split(".dash-incidents {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 3fr) minmax(0, 1fr)" in block
    assert ".is-selected td" in css


def test_rows_carry_asset_id_or_empty_for_no_host():
    db = _db()
    demo = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    with_asset = _add(db, asset=demo)
    no_asset = _add(db)
    done = _add(db, status="RESOLVED", asset=demo)
    numbers = (with_asset.number, no_asset.number, done.number)
    db.close()
    html = _client().get("/").text
    a = _row_attrs(html, numbers[0])
    assert a["data-asset"] == DEMO_ASSET and a["data-active"] == "true"
    b = _row_attrs(html, numbers[1])
    assert b["data-asset"] == "" and b["data-active"] == "true"
    c = _row_attrs(html, numbers[2])
    assert c["data-active"] == "false"
    js = JS.read_text(encoding="utf-8")
    graphs = js.split("(function bindDashGraphs() {", 1)[1].split("\n})();", 1)[0]
    graphs += js.split("\nfunction graphPane(", 1)[1].split("\n}\n", 1)[0]
    assert '"/api/v1/assets/" + encodeURIComponent(asset) + "/metrics"' in graphs
    assert "No host to chart" in graphs
    assert "No samples yet" in graphs


def test_dashboard_html_has_no_grafana_embed():
    db = _db()
    demo = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    _add(db, asset=demo)
    db.close()
    html = _client().get("/").text
    assert "<iframe" not in html.lower()
    pane = html[html.index("data-dash-graphs") : html.index("</aside>", html.index("data-dash-graphs"))]
    assert "grafana" not in pane.lower()
    js = JS.read_text(encoding="utf-8")
    graphs = js.split("(function bindDashGraphs() {", 1)[1].split("\n})();", 1)[0]
    graphs += js.split("\nfunction graphPane(", 1)[1].split("\n}\n", 1)[0]
    assert "grafana" not in graphs.lower() and "iframe" not in graphs.lower()


def test_metrics_route_blocks_anonymous():
    _db().close()
    anon = TestClient(app).get(f"/api/v1/assets/{DEMO_ASSET}/metrics")
    assert anon.status_code == 401


def test_panel_tiles_carry_raw_series_for_charts():
    def query(expr: str) -> dict:
        if expr.startswith("up{"):
            return {"value": 1.0, "query": expr}
        return {"value": 40.0, "query": expr}

    def ranged(expr: str) -> dict:
        if expr.startswith("up{"):
            return {"values": [1, 1, 0, 1]}
        if "node_cpu" in expr:
            return {"values": [10, 20, 30]}
        if "node_memory" in expr:
            return {"values": [55.5]}
        return {"values": []}

    panel = asset_metric_panel(
        {"asset_id": "app-graph-01", "type": "Linux Server"}, query_fn=query, range_fn=ranged
    )
    by_key = {tile["key"]: tile for tile in panel["tiles"]}
    assert by_key["up"]["series"] == [1.0, 1.0, 0.0, 1.0]
    assert by_key["cpu_percent"]["series"] == [10.0, 20.0, 30.0]
    assert by_key["memory_percent"]["series"] == [55.5]
    assert by_key["disk_percent"]["series"] == []

    network = asset_metric_panel(
        {"asset_id": "sw-graph-01", "type": "Network Switch"}, query_fn=query, range_fn=ranged
    )
    assert [tile["key"] for tile in network["tiles"]] == ["up"]
    assert 'job="forgesre-snmp"' in network["tiles"][0]["query"]


_HARNESS = r"""
const SRC = process.argv[1];
const ROWS = JSON.parse(process.argv[2]);
const PANEL = JSON.parse(process.argv[3]);
function el(tag) {
  const e = {
    tagName: tag, attrs: {}, children: [], hidden: false, textContent: "", listeners: {}, classes: new Set(),
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return k in this.attrs; },
    append(...c) { this.children.push(...c); },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...c) { this.children = c; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    closest(sel) { return sel.startsWith("tr") && this.attrs["data-dash-incident"] != null ? this : null; },
  };
  e.classList = { toggle(c, on) { on ? e.classes.add(c) : e.classes.delete(c); }, add(c) { e.classes.add(c); } };
  return e;
}
const parts = {
  "[data-dash-graph-subject]": el("p"), "[data-dash-graph-empty]": el("p"),
  "[data-dash-graph-caption]": el("p"),
  "[data-dash-graph-list]": el("div"), "[data-dash-graph-asset]": el("a"),
};
const pane = el("aside"); pane.querySelector = (s) => parts[s] || null;
const rows = ROWS.map((r) => {
  const tr = el("tr");
  tr.attrs = { "data-dash-incident": r.number, "data-asset": r.asset, "data-host": r.host || "", "data-active": r.active };
  return tr;
});
const table = el("table"); table.querySelectorAll = () => rows;
table.closest = () => null;
const navigated = [];
globalThis.document = {
  querySelector: (s) => (s === "[data-dash-graphs]" ? pane : s === "[data-dash-incident-table]" ? table : null),
  createElement: el, createElementNS: (_ns, tag) => el(tag), addEventListener() {},
};
globalThis.window = {
  setInterval: () => 1, clearInterval: () => {}, setTimeout, clearTimeout,
  location: { assign: (u) => navigated.push(u) },
};
const fetched = [];
globalThis.fetch = (url) => { fetched.push(url); return Promise.resolve({ ok: true, json: () => Promise.resolve(PANEL) }); };
eval(SRC);
const click = (i) => table.listeners.click.forEach((fn) => fn({ target: rows[i] }));
const snap = () => ({
  fetched: fetched.slice(),
  selected: rows.findIndex((r) => r.classes.has("is-selected")),
  empty: parts["[data-dash-graph-empty]"].textContent,
  charts: parts["[data-dash-graph-list]"].children.length,
  points: parts["[data-dash-graph-list]"].children.map((box) => {
    const svg = (box.children || []).find((child) => child.tagName === "svg");
    if (!svg) return "";
    const poly = (svg.children || []).find((child) => child.attrs && child.attrs.points);
    return poly ? poly.attrs.points : "";
  }),
  markerX: parts["[data-dash-graph-list]"].children.map((box) => {
    const svg = (box.children || []).find((child) => child.tagName === "svg");
    if (!svg) return null;
    const line = (svg.children || []).find((child) => child.attrs && String(child.attrs.class || "").indexOf("dash-chart-marker") >= 0);
    return line ? Number(line.attrs.x1) : null;
  }),
  caption: parts["[data-dash-graph-caption]"].textContent,
  navigated: navigated.slice(),
});
const out = {};
setTimeout(() => {
  out.initial = snap();
  click(ROWS.length - 1);
  out.noAsset = snap();
  click(0);
  setTimeout(() => { out.reselect = snap(); console.log(JSON.stringify(out)); }, 20);
}, 20);
"""


def _run_js(rows: list[dict], panel: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = JS.read_text(encoding="utf-8")
    picker = "function listPicker(" + src.split("\nfunction listPicker(", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    picker += "function graphPane(" + src.split("\nfunction graphPane(", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    block = picker + "(function bindDashGraphs() {" + src.split("(function bindDashGraphs() {", 1)[1].split("\n})();", 1)[0] + "\n})();"
    proc = subprocess.run(
        [node, "-e", _HARNESS, "--", block, json.dumps(rows), json.dumps(panel)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_js_selects_first_active_and_requests_its_metrics():
    rows = [
        {"number": "INC-9", "asset": "srv-old", "active": "false"},
        {"number": "INC-8", "asset": DEMO_ASSET, "active": "true"},
        {"number": "INC-7", "asset": "", "host": "10.0.0.9", "active": "true"},
    ]
    panel = {
        "collecting": True,
        "collecting_line": "Prometheus sees this target (up=1).",
        "error": "",
        "tiles": [
            {"key": "up", "name": "Collecting", "kind": "up", "tone": "ok", "value": 1, "display": "up", "series": [1, 1, 1]},
            {"key": "cpu_percent", "name": "CPU", "kind": "percent", "tone": "ok", "value": 12, "display": "12%", "threshold": 80, "series": [12]},
        ],
    }
    out = _run_js(rows, panel)
    assert out["initial"]["selected"] == 1
    assert out["initial"]["fetched"] == [f"/api/v1/assets/{DEMO_ASSET}/metrics?incident=INC-8"]
    assert out["initial"]["charts"] == 2
    assert out["noAsset"]["selected"] == 2
    assert out["noAsset"]["fetched"] == out["initial"]["fetched"]
    assert out["noAsset"]["empty"] == "No host to chart"
    assert out["noAsset"]["charts"] == 0
    assert out["reselect"]["selected"] == 0
    assert out["reselect"]["fetched"][-1] == "/api/v1/assets/srv-old/metrics?incident=INC-9"
    assert out["reselect"]["navigated"] == []


def test_js_one_point_series_says_no_samples_yet():
    rows = [{"number": "INC-1", "asset": DEMO_ASSET, "active": "true"}]
    panel = {
        "collecting": False,
        "collecting_line": "Prometheus is not collecting this target.",
        "error": "",
        "tiles": [{"key": "up", "name": "Collecting", "kind": "up", "tone": "warn", "value": None, "display": "not collecting", "series": [1]}],
    }
    out = _run_js(rows, panel)
    assert out["initial"]["empty"] == "Not scraped."
    assert out["initial"]["points"] == [""]


def _ys(points: str) -> list[float]:
    return [float(pair.split(",")[1]) for pair in points.split() if "," in pair]


def test_js_small_cpu_swing_is_not_a_flat_line():
    rows = [{"number": "INC-1", "asset": "app-01", "active": "true"}]
    panel = {
        "collecting": True,
        "collecting_line": "Prometheus sees this target (up=1).",
        "source": "prometheus",
        "tiles": [
            {
                "key": "cpu_percent",
                "name": "CPU",
                "kind": "percent",
                "tone": "ok",
                "value": 14,
                "display": "14%",
                "threshold": 95,
                "alarm_enabled": True,
                "series": [10, 14, 11],
            }
        ],
    }
    out = _run_js(rows, panel)
    ys = _ys(out["initial"]["points"][0])
    assert max(ys) - min(ys) > 10


def test_graph_query_uses_sd_scrape_identity():
    from app.inventory import create_manual_asset, sd_targets

    db = _db()
    token = uuid4().hex[:8]
    host = f"lnx-g-{token}"
    n = int(token[:4], 16)
    ip = f"10.{200 + (n % 40)}.{(n // 40) % 250}.{1 + (n % 200)}"
    asset = create_manual_asset(db, hostname=host, ip=ip, type="Linux Server", actor="tester")
    asset_id = asset.asset_id
    try:
        row = next(item for item in sd_targets(db) if item["labels"]["asset"] == asset_id)
        target = row["targets"][0]
        job = row["labels"]["job"]
        assert target == asset.scrape_address
        assert job == "linux-standard"

        def query(expr: str) -> dict:
            matched = f'job="{job}"' in expr and f'instance="{target}"' in expr
            if not matched:
                return {"value": None, "query": expr}
            if "node_cpu" in expr:
                return {"value": 22.0, "query": expr}
            if "node_memory" in expr:
                return {"value": 33.0, "query": expr}
            if "node_filesystem" in expr:
                return {"value": 44.0, "query": expr}
            if "up{" in expr:
                return {"value": 1.0, "query": expr}
            return {"value": None, "query": expr}

        def ranged(expr: str) -> dict:
            if "node_cpu" in expr and f'instance="{target}"' in expr:
                return {"values": [10.0, 18.0, 14.0]}
            if "up{" in expr and f'instance="{target}"' in expr:
                return {"values": [1.0, 1.0, 1.0]}
            return {"values": []}

        panel = asset_metric_panel(asset, query_fn=query, range_fn=ranged)
    finally:
        db.query(Asset).filter_by(asset_id=asset_id).delete(synchronize_session=False)
        db.commit()
        db.close()
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["series"] == [10.0, 18.0, 14.0]
    assert f'job="{job}"' in cpu["query"]
    assert f'instance="{target}"' in cpu["query"]


def test_snmp_graph_instance_is_the_sd_target():
    from app.inventory import create_manual_asset, sd_snmp_targets
    from rca.collector import promql_selectors_for

    db = _db()
    token = uuid4().hex[:8]
    n = int(token[:4], 16)
    ip = f"10.{80 + (n % 40)}.{(n // 40) % 250}.{1 + (n % 200)}"
    asset = create_manual_asset(
        db, hostname=f"sw-g-{token}", ip=ip, type="Network device", actor="tester", snmp_port=1161
    )
    asset_id = asset.asset_id
    try:
        row = next(item for item in sd_snmp_targets(db) if item["labels"]["asset"] == asset_id)
        target = row["targets"][0]
        assert target == f"{ip}:1161"
        selectors = promql_selectors_for(
            {
                "asset_id": asset_id,
                "ip": ip,
                "type": "Network device",
                "monitoring_profile": asset.monitoring_profile,
                "snmp_port": 1161,
            }
        )
    finally:
        db.query(Asset).filter_by(asset_id=asset_id).delete(synchronize_session=False)
        db.commit()
        db.close()
    assert selectors[0] == f'instance="{target}"'
    panel = asset_metric_panel(
        {
            "asset_id": asset_id,
            "ip": ip,
            "type": "Network device",
            "monitoring_profile": "network-switch",
            "snmp_port": 1161,
        },
        query_fn=lambda expr: {"value": 1.0, "query": expr} if f'instance="{target}"' in expr else {"value": None, "query": expr},
        range_fn=lambda expr: {"values": [1.0, 0.0, 1.0]} if f'instance="{target}"' in expr else {"values": []},
    )
    up = panel["tiles"][0]
    assert f'job="forgesre-snmp"' in up["query"]
    assert f'instance="{target}"' in up["query"]
    assert up["series"] == [1.0, 0.0, 1.0]


def test_range_is_kept_when_the_instant_sample_is_missing():
    def query(expr: str) -> dict:
        if "up{" in expr:
            return {"value": 0.0, "query": expr}
        return {"value": None, "query": expr}

    def ranged(expr: str) -> dict:
        if "node_cpu" in expr:
            return {"values": [10.0, 40.0, 70.0]}
        if "up{" in expr:
            return {"values": [1.0, 1.0, 0.0]}
        return {"values": []}

    panel = asset_metric_panel(
        {
            "asset_id": "app-down-01",
            "type": "Linux Server",
            "monitoring_profile": "linux-standard",
            "scrape_address": "10.51.2.9:9100",
        },
        query_fn=query,
        range_fn=ranged,
    )
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["series"] == [10.0, 40.0, 70.0]
    assert cpu["value"] == 70.0


def test_disabled_alarm_does_not_drop_the_graph_series():
    assert "bundled_alert_skip_reason" not in (ROOT / "backend" / "app" / "asset_metrics.py").read_text(encoding="utf-8")
    panel = asset_metric_panel(
        {
            "asset_id": "app-mute-01",
            "type": "Linux Server",
            "monitoring_profile": "linux-standard",
            "scrape_address": "10.51.2.10:9100",
            "alarms": {"cpu_percent": {"enabled": False, "threshold": 50}},
        },
        query_fn=lambda expr: {"value": 22.0, "query": expr} if ("node_cpu" in expr or "up{" in expr) else {"value": 10.0, "query": expr},
        range_fn=lambda expr: {"values": [10.0, 22.0, 18.0]} if "node_cpu" in expr else {"values": [1.0, 1.0, 1.0]},
    )
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["alarm_enabled"] is False
    assert cpu["series"] == [10.0, 22.0, 18.0]


def test_query_range_prefers_the_series_that_moves_and_reads_demo_history(monkeypatch):
    from app.metrics import set_demo_cpu
    from app.services import query_prometheus_range

    set_demo_cpu(12)
    seen: dict[str, str] = {}

    class _Resp:
        def __init__(self, payload: dict):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            del url
            query = (params or {}).get("query") or ""
            seen["query"] = query
            if query == "forgesre_demo_cpu_percent":
                payload = {"data": {"result": [{"values": [[1, "12"], [2, "40"], [3, "94"]]}]}}
            else:
                payload = {
                    "data": {
                        "result": [
                            {"values": [[1, "1"], [2, "1"], [3, "1"]]},
                            {"values": [[1, "10"], [2, "55"], [3, "12"]]},
                        ]
                    }
                }
            return _Resp(payload)

    monkeypatch.setattr("app.services.httpx.Client", _Client)
    history = query_prometheus_range("forgesre_demo_cpu_percent")
    assert seen["query"] == "forgesre_demo_cpu_percent"
    assert history["values"] == [12.0, 40.0, 94.0]
    picked = query_prometheus_range('node_cpu_seconds_total{job="linux-standard",instance="10.1.1.1:9100"}')
    assert picked["values"] == [10.0, 55.0, 12.0]
    assert picked["times"] == [1.0, 2.0, 3.0]
    assert history["times"] == [1.0, 2.0, 3.0]


def test_js_empty_window_says_no_samples_yet():
    rows = [{"number": "INC-1", "asset": DEMO_ASSET, "active": "true"}]
    panel = {
        "collecting": True,
        "collecting_line": "Prometheus sees this target (up=1).",
        "source": "prometheus",
        "window": {"start": 0, "end": 100, "marker": 40, "label": "10:00–11:00"},
        "tiles": [
            {"key": "cpu_percent", "name": "CPU", "kind": "percent", "tone": "ok", "value": 12, "display": "12%", "series": [12]},
        ],
    }
    out = _run_js(rows, panel)
    assert out["initial"]["empty"] == "No samples yet."
    assert out["initial"]["charts"] == 1
    assert out["initial"]["points"] == [""]
    assert out["initial"]["markerX"] == [None]
    assert out["initial"]["caption"] == "10:00–11:00"


def test_js_marker_and_drop_stay_on_the_incident_clock():
    """A series that dies at the marker must not be stretched to 'now'."""
    rows = [{"number": "INC-1", "asset": "app-01", "active": "true"}]
    panel = {
        "collecting": False,
        "collecting_line": "Prometheus is not collecting this target (up=0).",
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
            },
            {
                "key": "memory_percent",
                "name": "Memory",
                "kind": "percent",
                "tone": "crit",
                "value": 100,
                "display": "100%",
                "threshold": 90,
                "alarm_enabled": True,
                "series": [20, 20, 100],
                "times": [0, 30, 100],
            },
        ],
    }
    out = _run_js(rows, panel)
    assert out["initial"]["caption"] == "11:15–13:15"
    assert out["initial"]["markerX"] == [100.0, 100.0]
    cpu_pts = out["initial"]["points"][0].split()
    xs = [float(pair.split(",")[0]) for pair in cpu_pts]
    ys = [float(pair.split(",")[1]) for pair in cpu_pts]
    assert xs[0] == pytest.approx(0.0, abs=0.2)
    assert xs[-1] == pytest.approx(140.0, abs=0.6)
    assert xs[-1] < 190
    assert ys[-1] - ys[0] > 15
    mem_ys = _ys(out["initial"]["points"][1])
    assert mem_ys[0] - mem_ys[-1] > 15

    quiet = dict(panel)
    quiet["window"] = {"start": 0, "end": 100, "marker": None, "label": "10:00–11:00"}
    unmarked = _run_js(rows, quiet)
    assert unmarked["initial"]["markerX"] == [None, None]
