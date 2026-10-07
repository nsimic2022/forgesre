"""Incident detail: ← Older / Newer → in the /incidents order, and Engineer evidence as a When / Source / Action list."""

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

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Evidence, Incident
from app.seed import seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "static" / "app.js"
CSS = ROOT / "frontend" / "static" / "app.css"
TAG = "incnav:"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _drop_rows():
    yield
    db = _db()
    ids = [row.id for row in db.query(Incident.id).filter(Incident.fingerprint.like(f"{TAG}%"))]
    if ids:
        db.query(Evidence).filter(Evidence.incident_id.in_(ids)).delete(synchronize_session=False)
        db.query(Incident).filter(Incident.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add(statuses: list[str]) -> list[str]:
    db = _db()
    numbers = []
    for i, status in enumerate(statuses):
        row = Incident(
            number=next_incident_number(db),
            title=f"Nav walk {i} {uuid4().hex[:4]}",
            severity="WARNING",
            status=status,
            fingerprint=f"{TAG}{uuid4().hex}",
            started_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        numbers.append(row.number)
    db.close()
    return numbers


def _nav(html: str) -> str:
    start = html.index("data-incident-nav")
    return html[html.rindex("<nav", 0, start) : html.index("</nav>", start) + len("</nav>")]


def _step(nav: str, which: str) -> tuple[str, str | None]:
    """('a', href) for a live link, ('span', None) when disabled."""
    m = re.search(r"<(a|span) [^>]*data-incident-" + which + r"[^>]*>", nav)
    assert m, which
    tag = m.group(0)
    href = re.search(r'href="([^"]*)"', tag)
    return m.group(1), href.group(1) if href else None


def test_older_newer_walk_the_list_order_and_land_on_the_neighbour():
    first, middle, last = _add(["OPEN", "OPEN", "OPEN"])
    client = _client()
    html = client.get(f"/incidents/{middle}").text
    nav = _nav(html)
    assert html.count("data-incident-nav") == 1
    assert "← Older" in nav and "Newer →" in nav
    assert _step(nav, "older") == ("a", f"/incidents/{first}")
    assert _step(nav, "newer") == ("a", f"/incidents/{last}")

    older = client.get(f"/incidents/{first}")
    assert older.status_code == 200
    assert f'<code class="incident-id muted">{first}</code>' in older.text
    assert _step(_nav(older.text), "newer") == ("a", f"/incidents/{middle}")

    newest = client.get(f"/incidents/{last}").text
    kind, href = _step(_nav(newest), "newer")
    assert kind == "span" and href is None
    assert re.search(r'<span [^>]*pager-disabled[^>]*aria-disabled="true"[^>]*data-incident-newer', _nav(newest))
    assert _step(_nav(newest), "older") == ("a", f"/incidents/{middle}")


def test_nav_sits_right_above_the_header_strip_and_back_links_stay_on_top():
    _, number = _add(["OPEN", "OPEN"])
    html = _client().get(f"/incidents/{number}").text
    back = html.index('">← Incidents</a> · <a href="/history">History</a></p>')
    nav_start = html.rindex("<nav", 0, html.index("data-incident-nav"))
    nav_end = html.index("</nav>", nav_start) + len("</nav>")
    assert back < nav_start
    assert html[nav_end:].lstrip().startswith('<header class="incident-strip')
    assert html.count('class="pager-bar"') == html.count("data-pager-size-form")


def test_filter_from_the_list_is_kept_while_walking():
    first, resolved, last = _add(["OPEN", "RESOLVED", "OPEN"])
    client = _client()
    listed = client.get("/incidents?status=OPEN").text
    assert f'href="/incidents/{last}?status=OPEN"' in listed
    assert 'href="/incidents/' + last + '"' not in listed

    nav = _nav(client.get(f"/incidents/{last}?status=OPEN").text)
    assert _step(nav, "older") == ("a", f"/incidents/{first}?status=OPEN")
    assert _step(nav, "newer")[0] == "span"
    back = client.get(f"/incidents/{last}?status=OPEN").text
    assert '<a href="/incidents?status=OPEN">← Incidents</a>' in back

    unfiltered = _nav(client.get(f"/incidents/{last}").text)
    assert _step(unfiltered, "older") == ("a", f"/incidents/{resolved}")
    assert '<a href="/incidents">← Incidents</a>' in client.get(f"/incidents/{last}").text


def _with_evidence(n_rca: int) -> str:
    (number,) = _add(["OPEN"])
    db = _db()
    incident = db.query(Incident).filter_by(number=number).one()
    db.add(
        Evidence(
            incident_id=incident.id,
            kind="metrics",
            title="Metrics",
            payload={"cpu_percent": 92.5, "queries": {"cpu_percent": "avg(rate(node_cpu_seconds_total[5m]))"}},
            evidence_id="ROLLUP-metrics",
        )
    )
    for i in range(n_rca):
        db.add(
            Evidence(
                incident_id=incident.id,
                kind="METRIC",
                title=f"METRIC EV-{i + 1:05d}",
                payload={"evidence_id": f"EV-{i + 1:05d}", "type": "METRIC", "content": {"value": 90 + i, "unit": "%"}},
                evidence_id=f"EV-{i + 1:05d}",
                source="prometheus",
                query=f'up{{instance="h{i}"}}',
                hash=uuid4().hex,
            )
        )
    db.commit()
    db.close()
    return number


def _evidence(html: str) -> str:
    start = html.index('<section id="evidence">')
    return html[start : html.index("</section>", start)]


def test_engineer_evidence_is_a_when_source_action_list_not_a_pre_dump():
    number = _with_evidence(2)
    html = _client().get(f"/incidents/{number}").text
    ev = _evidence(html)
    assert "<pre" not in ev
    assert re.search(r'<div class="table-scroll list-scroll" data-list-scroll>\s*<table class="scan-list evidence-table" data-evidence-table>', ev)
    assert "<th class=\"ev-when\">When</th><th class=\"ev-actor\">Source</th><th>Action</th>" in ev
    rows = re.findall(r"<tr data-list-row data-evidence-row[^>]*>(.*?)</tr>", ev, flags=re.S)
    assert len(rows) == 3
    assert '<td class="ev-actor">ForgeRCA</td>' in rows[0]
    assert '<span class="ev-short">Metrics · cpu_percent=92.5, queries={…}</span>' in rows[0]
    assert '<td class="ev-actor">Prometheus</td>' in rows[1]
    assert '<span class="ev-short">METRIC EV-00001 · value=90, unit=%</span>' in rows[1]
    full = re.search(r'<div class="ev-full" data-evidence-full>(.*?)</div>', rows[1], flags=re.S).group(1)
    assert full.startswith("ID: EV-00001\nQuery: up{instance=&#34;h0&#34;}\n{")
    assert "&#34;content&#34;: {" in full and "&#34;value&#34;: 90" in full
    assert 'id="EV-00002"' in ev


def test_long_evidence_list_gets_its_own_rows_pager():
    number = _with_evidence(14)
    client = _client()
    ev = _evidence(client.get(f"/incidents/{number}").text)
    assert ev.count("data-evidence-row") == 10
    assert 'name="ev_per_page"' in ev
    assert "Showing 1–10 of 15" in ev
    assert f"/incidents/{number}?ev_page=2#evidence" in ev
    more = _evidence(client.get(f"/incidents/{number}?ev_per_page=20").text)
    assert more.count("data-evidence-row") == 15


def test_evidence_css_one_line_platform_font():
    css = CSS.read_text(encoding="utf-8")
    assert ".evidence-table { table-layout: fixed; }" in css
    short = css.split(".evidence-table td.ev-action .ev-short { overflow", 1)
    assert len(short) == 2 and "text-overflow: ellipsis; white-space: nowrap;" in short[1].split("}", 1)[0]
    full = css.split(".evidence-table .ev-full {", 1)[1].split("}", 1)[0]
    assert "display: none" in full and "font: inherit" in full and "white-space: pre-wrap" in full
    assert "monospace" not in full
    assert ".evidence-table tr.is-expanded .ev-full { display: block; }" in css


def _js_block(name: str) -> str:
    src = JS.read_text(encoding="utf-8")
    head = f"(function {name}() {{"
    assert head in src, name
    return head + src.split(head, 1)[1].split("\n})();", 1)[0] + "\n})();"


_DBL_HARNESS = r"""
const SRC = process.argv[1];
function el(tag, parent, attrs) {
  const e = {
    tagName: tag, parent: parent || null, attrs: attrs || {}, classes: new Set(), listeners: {},
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    closest(sel) {
      for (let n = this; n; n = n.parent) {
        if (sel.split(",").some((s) => {
          s = s.trim();
          if (s[0] === "[") return s.slice(1, -1) in n.attrs;
          return n.tagName === s;
        })) return n;
      }
      return null;
    },
  };
  e.classList = {
    contains: (c) => e.classes.has(c),
    toggle: (c, on) => { (on === undefined ? !e.classes.has(c) : on) ? e.classes.add(c) : e.classes.delete(c); },
  };
  return e;
}
const table = el("table", null, { "data-evidence-table": "" });
const row = el("tr", table, { "data-list-row": "", "data-evidence-row": "", "aria-expanded": "false" });
const cell = el("td", row);
const full = el("div", cell, { "data-evidence-full": "" });
globalThis.document = { querySelector: (s) => (s === "[data-evidence-table]" ? table : null) };
eval(SRC);
const fire = (type, target, extra) => {
  const ev = Object.assign({ target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, extra || {});
  (table.listeners[type] || []).forEach((fn) => fn(ev));
  return ev.defaultPrevented;
};
const state = () => ({ expanded: row.classes.has("is-expanded"), aria: row.attrs["aria-expanded"] });
const out = {};
const press = (target, x0, x1) => {
  fire("mousedown", target, { detail: 1, clientX: x0, clientY: 10 });
  fire("mouseup", target, { detail: 1, clientX: x1, clientY: 10 });
};
out.noClickListener = !("click" in table.listeners);
fire("dblclick", cell);
out.afterDbl = state();
press(full, 20, 20);
out.secondDownPrevented = fire("mousedown", full, { detail: 2, clientX: 20, clientY: 10 });
fire("dblclick", full);
out.dblOnOpenBody = state();
press(cell, 5, 5);
out.firstDownPrevented = fire("mousedown", cell, { detail: 1 });
fire("dblclick", cell);
out.afterSecondDbl = state();
press(full, 10, 120);
fire("dblclick", full);
out.afterDragDbl = state();
press(full, 30, 31);
fire("dblclick", full);
out.afterStillDbl = state();
press(full, 30, 30);
fire("dblclick", full);
out.afterCloseAgain = state();
out.enterPrevented = fire("keydown", row, { key: "Enter" });
out.afterEnter = state();
console.log(JSON.stringify(out));
"""


def test_double_click_anywhere_toggles_including_open_body_drag_does_not():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run(
        [node, "-e", _DBL_HARNESS, "--", _js_block("bindEvidenceRows")], capture_output=True, text=True, timeout=20
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    closed, opened = {"expanded": False, "aria": "false"}, {"expanded": True, "aria": "true"}
    assert out["noClickListener"] is True
    assert out["afterDbl"] == opened
    assert out["secondDownPrevented"] is True and out["firstDownPrevented"] is False
    assert out["dblOnOpenBody"] == closed
    assert out["afterSecondDbl"] == opened
    assert out["afterDragDbl"] == opened
    assert out["afterStillDbl"] == closed
    assert out["afterCloseAgain"] == opened
    assert out["enterPrevented"] is True
    assert out["afterEnter"] == closed


def test_evidence_js_no_longer_skips_the_open_body():
    block = _js_block("bindEvidenceRows")
    assert "[data-evidence-full]" not in block
    assert 'addEventListener("mouseup"' in block and "DRAG_PX" in block


def test_evidence_view_does_not_change_stored_payload():
    number = _with_evidence(1)
    db = _db()
    before = [(row.evidence_id, json.dumps(row.payload, sort_keys=True)) for row in db.query(Evidence).join(Incident).filter(Incident.number == number)]
    db.close()
    _client().get(f"/incidents/{number}")
    db = _db()
    after = [(row.evidence_id, json.dumps(row.payload, sort_keys=True)) for row in db.query(Evidence).join(Incident).filter(Incident.number == number)]
    db.close()
    assert sorted(before) == sorted(after)
