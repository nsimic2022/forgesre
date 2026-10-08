"""Every paginated list: inner scroll box, row click selects (no navigation), arrows stay in the box, identity link opens."""

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
from app.journal import report
from app.main import app
from app.models import Asset, Incident
from app.seed import DEMO_ASSET, seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"
JS = ROOT / "frontend" / "static" / "app.js"
CSS = ROOT / "frontend" / "static" / "app.css"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _drop_rows():
    yield
    db = _db()
    db.query(Incident).filter(Incident.fingerprint.like("listscroll:%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add_many(n: int, status: str = "OPEN") -> list[str]:
    db = _db()
    demo = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    numbers = []
    for i in range(n):
        row = Incident(
            number=next_incident_number(db),
            title=f"List scroll {i:02d} {uuid4().hex[:4]}",
            severity="WARNING",
            status=status,
            fingerprint=f"listscroll:{uuid4().hex}",
            started_at=datetime.now(timezone.utc),
            asset_id=demo.id,
        )
        db.add(row)
        db.commit()
        numbers.append(row.number)
    db.close()
    return numbers


def _boxes(html: str) -> list[str]:
    """Inner HTML of each list box (div.list-scroll ... matching </div>)."""
    out = []
    for m in re.finditer(r'<div class="[^"]*\blist-scroll\b[^"]*" data-list-scroll>', html):
        depth, i = 1, m.end()
        while depth:
            nxt_open = html.find("<div", i)
            nxt_close = html.find("</div>", i)
            if nxt_open != -1 and nxt_open < nxt_close:
                depth, i = depth + 1, nxt_open + 4
            else:
                depth, i = depth - 1, nxt_close + 6
        out.append(html[m.end() : i - 6])
    return out


def _rows(box: str) -> list[str]:
    return re.findall(r"<tr [^>]*data-list-row[^>]*>.*?</tr>", box, flags=re.S)


@pytest.mark.parametrize("path", ["/incidents"])
def test_incident_lists_scroll_inside_box_and_title_still_opens(path):
    numbers = _add_many(3)
    html = _client().get(path).text
    boxes = _boxes(html)
    assert len(boxes) == 1, path
    box = boxes[0]
    assert "<thead>" in box and "</table>" in box
    tail = html.split(box, 1)[1]
    assert tail.startswith("</div>")
    if 'class="pager-bar"' in html:
        assert html.index('class="pager-bar"') > html.index(box)
    rows = _rows(box)
    for number in numbers:
        row = next(r for r in rows if f'href="/incidents/{number}"' in r)
        tag = row.split(">", 1)[0]
        assert "href=" not in tag and "onclick" not in tag
        assert re.search(r'<a class="inc-title[^"]*" href="/incidents/' + re.escape(number) + r'"[^>]*data-list-open', row)
        assert f'href="/incidents/{number}" title="{number}"' in row


def test_assets_table_scrolls_inside_box_and_asset_id_opens():
    html = _client().get(f"/assets?q={DEMO_ASSET}").text
    boxes = _boxes(html)
    assert len(boxes) == 1
    box = boxes[0]
    assert '<table class="asset-table" data-asset-reachability>' in box
    rows = _rows(box)
    assert rows
    demo = next(r for r in rows if f'href="/assets/{DEMO_ASSET}"' in r)
    assert f'<a href="/assets/{DEMO_ASSET}" data-list-open>{DEMO_ASSET}</a>' in demo
    assert "href=" not in demo.split(">", 1)[0]


def test_edit_target_row_keeps_selected_class_inside_box():
    html = _client().get(f"/assets?edit={DEMO_ASSET}&q={DEMO_ASSET}").text
    rows = _rows(_boxes(html)[0])
    edited = next(r for r in rows if f'href="/assets/{DEMO_ASSET}"' in r)
    assert re.match(r'<tr class="selected" data-list-row>', edited)


def test_every_paginated_page_has_a_list_box_with_pickable_rows():
    _add_many(2)
    db = _db()
    report(db, "core", "list-scroll-test", summary="list scroll fixture")
    db.close()
    client = _client()
    pages = {
        "/": (1, True),
        "/journal": (1, True),
        "/discovery": (1, True),
        "/playrules": (1, True),
        "/ops": (2, False),
        "/admin": (2, True),
    }
    for path, (at_least, has_rows) in pages.items():
        html = client.get(path).text
        boxes = _boxes(html)
        assert len(boxes) >= at_least, path
        for box in boxes:
            assert "<table" in box, path
        if has_rows:
            assert any(_rows(box) for box in boxes), path
    ops = (TEMPLATES / "ops.html").read_text(encoding="utf-8")
    assert "<summary data-list-open>View</summary>" in ops
    assert ops.count("<tr data-list-row>") == 1 and ops.count(" data-list-row>") == 2
    admin = _boxes(client.get("/admin").text)[0]
    assert re.search(r'<a href="/admin\?selected=\d+[^"]*" data-list-open>', admin)
    dash = client.get("/").text
    journal = dash[dash.index('<section id="journal">') :]
    assert _boxes(journal) and "data-dash-list" not in journal


def test_card_lists_select_cards_inside_a_box():
    client = _client()
    for path, card in (("/playbooks", "playbook-card"), ("/escalation", "policy-card")):
        html = client.get(path).text
        boxes = [b for b in _boxes(html) if card in b]
        assert len(boxes) == 1, path
        cards = re.findall(r'<section class="card ' + card + r'[^"]*" data-list-row>', boxes[0])
        assert cards, path
    raw = (TEMPLATES / "playbooks.html").read_text(encoding="utf-8")
    assert '<div class="list-scroll list-scroll-cards" data-list-scroll>' in raw


def test_every_template_with_a_pager_wraps_its_list():
    skip = {"_pager.html", "dashboard.html"}
    for tpl in sorted(TEMPLATES.glob("*.html")):
        if tpl.name in skip:
            continue
        text = tpl.read_text(encoding="utf-8")
        if '"_pager.html"' not in text:
            continue
        assert "data-list-scroll" in text, tpl.name
        assert "data-list-row" in text, tpl.name
    dash = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert dash.count("data-list-scroll") == 1 and "data-dash-list" in dash


def test_css_box_scrolls_with_sticky_header_and_selection_style():
    css = CSS.read_text(encoding="utf-8")
    block = css.split(".list-scroll {", 1)[1].split("}", 1)[0]
    assert "overflow: auto" in block and "max-height:" in block
    assert ".list-scroll > table > thead th { position: sticky; top: 0" in css
    assert "tr[data-list-row].is-selected td {" in css
    assert ".card[data-list-row].is-selected {" in css
    assert "[data-list-row]:focus-visible" in css
    assert "tr[data-dash-incident].is-selected" not in css


def _js_fn(name: str) -> str:
    src = JS.read_text(encoding="utf-8")
    head = f"\nfunction {name}("
    assert head in src, name
    return head.lstrip() + src.split(head, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def _js_block(name: str) -> str:
    src = JS.read_text(encoding="utf-8")
    head = f"(function {name}() {{"
    assert head in src, name
    return head + src.split(head, 1)[1].split("\n})();", 1)[0] + "\n})();"


def test_one_shared_picker_used_by_dashboard_and_every_list():
    src = JS.read_text(encoding="utf-8")
    assert src.count("function listPicker(") == 1
    assert src.count("const STEP = {") == 1
    assert "listPicker(table," in _js_block("bindDashGraphs")
    assert "listPicker(box)" in _js_block("bindListPick")
    assert src.index("(function bindDashGraphs()") < src.index("(function bindListPick()")


_HARNESS = r"""
const SRC = process.argv[1];
const N = Number(process.argv[2]);
const START = Number(process.argv[3]);
const ROW_H = 40, HEAD = 30, BOX_TOP = 100;
function matches(e, simple) {
  const m = simple.trim().match(/^([a-z]*)((?:\[[^\]]+\]|\.[\w-]+)*)$/);
  if (!m) return false;
  if (m[1] && e.tagName !== m[1]) return false;
  for (const part of m[2].match(/\[[^\]]+\]|\.[\w-]+/g) || []) {
    if (part[0] === ".") { if (!e.classes.has(part.slice(1))) return false; }
    else if (!(part.slice(1, -1).split("=")[0] in e.attrs)) return false;
  }
  return true;
}
function el(tag, parent) {
  const e = {
    tagName: tag, parent: parent || null, attrs: {}, children: [], classes: new Set(), listeners: {},
    scrollTop: 0, style: {}, clicks: 0,
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return k in this.attrs; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    closest(sel) {
      for (let n = this; n; n = n.parent) if (sel.split(",").some((s) => matches(n, s))) return n;
      return null;
    },
    contains(o) { for (let n = o; n; n = n.parent) if (n === this) return true; return false; },
    querySelector(sel) {
      for (const c of this.children) {
        if (matches(c, sel)) return c;
        const deep = c.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
    focus(opts) { document.activeElement = this; this.focusOpts = opts || {}; },
    blur() { if (document.activeElement === this) document.activeElement = body; },
    click() { this.clicks++; },
  };
  e.classList = {
    toggle(c, on) { on ? e.classes.add(c) : e.classes.delete(c); },
    add(c) { e.classes.add(c); },
    contains(c) { return e.classes.has(c); },
  };
  if (parent) parent.children.push(e);
  return e;
}
const body = el("body");
const outside = el("p", body);
const box = el("div", body);
box.attrs["data-list-scroll"] = "";
const boxH = () => (box.style.maxHeight ? Number(box.style.maxHeight.match(/(\d+)px/)[1]) : HEAD + N * ROW_H);
box.getBoundingClientRect = () => ({ top: BOX_TOP, bottom: BOX_TOP + boxH(), height: boxH() });
box.offsetHeight = 0; box.clientHeight = 0;
const table = el("table", box);
table.tHead = { getBoundingClientRect: () => ({ height: HEAD }) };
const realQS = box.querySelector.bind(box);
box.querySelector = (s) => (s === ":scope > table" ? table : realQS(s));
const rows = [];
for (let i = 0; i < N; i++) {
  const tr = el("tr", table);
  tr.attrs["data-list-row"] = "";
  if (i === START) tr.classes.add("selected");
  tr.getBoundingClientRect = () => {
    const top = BOX_TOP + HEAD + i * ROW_H - box.scrollTop;
    return { top, bottom: top + ROW_H, height: ROW_H };
  };
  const td = el("td", tr);
  const input = el("input", td);
  const button = el("button", td);
  tr.td = td; tr.input = input; tr.button = button;
  if (i % 2 === 0) {
    const link = el("a", td);
    link.attrs["data-list-open"] = "";
    link.href = "http://h/things/" + i;
    tr.link = link;
  } else if (i === 1) {
    const summary = el("summary", td);
    summary.attrs["data-list-open"] = "";
    tr.summary = summary;
  }
  rows.push(tr);
}
box.querySelectorAll = (s) => (s === "[data-list-row]" ? rows : []);
const docListeners = {};
const navigated = [];
const windowScrolls = [];
let selection = "";
globalThis.document = {
  activeElement: body, readyState: "complete",
  querySelectorAll: (s) => (s === "[data-list-scroll]" ? [box] : []),
  addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
};
globalThis.window = {
  location: { assign: (u) => navigated.push(u) },
  scrollTo: (...a) => windowScrolls.push(a), scrollBy: (...a) => windowScrolls.push(a),
  requestAnimationFrame: (fn) => { fn(); return 1; },
  addEventListener() {},
  getSelection: () => selection,
};
eval(SRC);
const key = (k, target) => {
  const ev = { key: k, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  box.listeners.keydown.forEach((fn) => fn(ev));
  return ev.defaultPrevented;
};
const click = (target) => box.listeners.click.forEach((fn) => fn({ target }));
const down = (target) => (docListeners.mousedown || []).forEach((fn) => fn({ target }));
const sel = () => rows.findIndex((r) => r.classes.has("is-selected"));
const out = {};
out.bound = box.hasAttribute("data-list-bound");
out.maxHeight = box.style.maxHeight || "";
out.initial = { selected: sel(), scrollTop: box.scrollTop, tabs: rows.map((r) => r.tabIndex).slice(0, 4) };
click(rows[2].td);
out.click = { selected: sel(), focused: rows.indexOf(document.activeElement), navigated: navigated.length };
click(rows[4].link);
click(rows[4].button);
click(rows[4].input);
out.controlsIgnored = sel();
selection = "copied text";
click(rows[5].td);
out.dragIgnored = sel();
selection = "";
out.down = key("ArrowDown", rows[2]);
out.j = (key("j", rows[3]), sel());
out.k = (key("k", rows[4]), sel());
out.end = key("End", rows[3]);
out.afterEnd = { selected: sel(), scrollTop: box.scrollTop, bottom: rows[N - 1].getBoundingClientRect().bottom, boxBottom: box.getBoundingClientRect().bottom };
out.home = key("Home", rows[N - 1]);
out.afterHome = { selected: sel(), scrollTop: box.scrollTop };
out.inInput = key("ArrowDown", rows[0].input);
out.inInputSelected = sel();
out.enterOpen = key("Enter", rows[0]);
out.navAfterEnter = navigated.slice();
key("ArrowDown", rows[0]);
out.enterSummary = key("Enter", rows[1]);
out.summaryClicks = rows[1].summary.clicks;
key("ArrowDown", rows[1]);
key("ArrowDown", rows[2]);
out.enterNothing = key("Enter", rows[3]);
out.enterOnButton = key("Enter", rows[3].button);
out.navTotal = navigated.length;
down(rows[3].td);
out.downInside = rows.indexOf(document.activeElement);
down(outside);
out.downOutside = document.activeElement === body;
out.windowScrolls = windowScrolls.length;
console.log(JSON.stringify(out));
"""


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    return node


def _run(n: int, start: int) -> dict:
    src = _js_fn("listPicker") + _js_block("bindListPick")
    proc = subprocess.run([_node(), "-e", _HARNESS, "--", src, str(n), str(start)], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_js_row_click_selects_without_navigating_and_keys_stay_in_box():
    out = _run(24, -1)
    assert out["bound"] is True
    assert out["maxHeight"] == f"min({30 + 10 * 40 + 1}px, 72vh)"
    assert out["initial"] == {"selected": -1, "scrollTop": 0, "tabs": [0, -1, -1, -1]}
    assert out["click"] == {"selected": 2, "focused": 2, "navigated": 0}
    assert out["controlsIgnored"] == 2
    assert out["dragIgnored"] == 2
    assert out["down"] is True and out["j"] == 4 and out["k"] == 3
    assert out["end"] is True
    end = out["afterEnd"]
    assert end["selected"] == 23 and end["scrollTop"] > 0 and end["bottom"] <= end["boxBottom"]
    assert out["home"] is True and out["afterHome"] == {"selected": 0, "scrollTop": 0}
    assert out["inInput"] is False and out["inInputSelected"] == 0
    assert out["enterOpen"] is True and out["navAfterEnter"] == ["http://h/things/0"]
    assert out["enterSummary"] is True and out["summaryClicks"] == 1
    assert out["enterNothing"] is False and out["enterOnButton"] is False
    assert out["navTotal"] == 1
    assert out["downInside"] == 3
    assert out["downOutside"] is True
    assert out["windowScrolls"] == 0


def test_js_short_list_keeps_css_height_and_edit_row_starts_selected():
    out = _run(8, 5)
    assert out["maxHeight"] == ""
    assert out["initial"]["selected"] == 5
    assert out["initial"]["tabs"] == [-1, -1, -1, -1]
    long = _run(30, 27)
    assert long["initial"]["selected"] == 27
    assert long["initial"]["scrollTop"] > 0
