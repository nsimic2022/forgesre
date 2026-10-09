"""Dashboard Recent incidents: row click selects for graphs, arrows stay in the list. Pagers keep the scroll position."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset, Incident
from app.seed import DEMO_ASSET, seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
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
    db.query(Incident).filter(Incident.fingerprint.like("dashscroll:%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _add_many(n: int) -> list[str]:
    db = _db()
    demo = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    numbers = []
    for i in range(n):
        row = Incident(
            number=next_incident_number(db),
            title=f"Dash scroll {i:02d} {uuid4().hex[:4]}",
            severity="WARNING",
            status="OPEN",
            fingerprint=f"dashscroll:{uuid4().hex}",
            started_at=datetime.now(timezone.utc),
            asset_id=demo.id,
        )
        db.add(row)
        db.commit()
        numbers.append(row.number)
    db.close()
    return numbers


def _recent(html: str) -> str:
    start = html.index("<h2>Recent incidents</h2>")
    return html[start : html.index("data-dash-graphs", start)]


def _js_block(name: str) -> str:
    src = JS.read_text(encoding="utf-8")
    head = f"(function {name}() {{"
    assert head in src, name
    return head + src.split(head, 1)[1].split("\n})();", 1)[0] + "\n})();"


def _js_fn(name: str) -> str:
    src = JS.read_text(encoding="utf-8")
    head = f"\nfunction {name}("
    assert head in src, name
    return head.lstrip() + src.split(head, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def test_recent_rows_select_and_title_still_links_to_detail():
    numbers = _add_many(3)
    html = _client().get("/").text
    recent = _recent(html)
    for number in numbers:
        tr = re.search(r'<tr [^>]*data-dash-incident="' + re.escape(number) + r'"[^>]*>', recent)
        assert tr is not None, number
        tag = tr.group(0)
        assert 'data-dash-select="graphs"' in tag
        assert "dash-pick" in tag
        assert "href=" not in tag and "onclick" not in tag
        row = recent[tr.end() : recent.index("</tr>", tr.end())]
        assert re.search(r'<a class="inc-title[^"]*" href="/incidents/' + re.escape(number) + '"', row)
        assert f'href="/incidents/{number}" title="{number}"' in row
    tabs = re.findall(r'<tr [^>]*data-dash-incident="[^"]*"[^>]*tabindex="(-?\d)"', recent)
    assert tabs.count("0") == 1 and tabs[0] == "0"
    assert set(tabs[1:]) <= {"-1"}


def test_list_scrolls_inside_its_box_with_pager_outside():
    _add_many(1)
    recent = _recent(_client().get("/").text)
    box = recent.index('class="dash-recent-scroll" data-dash-list')
    table = recent.index("data-dash-incident-table")
    closing = recent.index("</table>")
    assert box < table < closing
    assert recent[closing + len("</table>") :].lstrip().startswith("</div>")
    if 'class="pager-bar"' in recent:
        assert recent.index('class="pager-bar"') > closing
    css = CSS.read_text(encoding="utf-8")
    block = css.split(".dash-recent-scroll {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in block and "max-height:" in block
    assert ".dash-recent-scroll thead th { position: sticky" in css
    assert ".dash-recent a.inc-title { display: inline-block" in css


_KEYS_HARNESS = r"""
const SRC = process.argv[1];
const N = Number(process.argv[2]);
const ROW_H = 40, HEAD = 30, BOX_TOP = 100, BOX_H = 200;
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
    hidden: false, textContent: "", tabIndex: 0, scrollTop: 0, href: "",
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return k in this.attrs; },
    append(...c) { this.children.push(...c); },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...c) { this.children = c; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    closest(sel) {
      for (let n = this; n; n = n.parent) if (sel.split(",").some((s) => matches(n, s))) return n;
      return null;
    },
    contains(o) { for (let n = o; n; n = n.parent) if (n === this) return true; return false; },
    querySelector(sel) {
      for (const c of this.children) {
        if (c && c.tagName && matches(c, sel)) return c;
        const deep = c && c.querySelector ? c.querySelector(sel) : null;
        if (deep) return deep;
      }
      return null;
    },
    focus(opts) { document.activeElement = this; this.focusOpts = opts || {}; },
    blur() { if (document.activeElement === this) document.activeElement = body; },
  };
  e.classList = {
    toggle(c, on) { on ? e.classes.add(c) : e.classes.delete(c); },
    add(c) { e.classes.add(c); },
  };
  if (parent) parent.children.push(e);
  return e;
}
const body = el("body");
const outside = el("p", body);
const scroller = el("div", body);
scroller.attrs["data-dash-list"] = "";
scroller.getBoundingClientRect = () => ({ top: BOX_TOP, bottom: BOX_TOP + BOX_H, height: BOX_H });
const table = el("table", scroller);
table.attrs["data-dash-incident-table"] = "";
table.tHead = { getBoundingClientRect: () => ({ height: HEAD }) };
const rows = [];
for (let i = 0; i < N; i++) {
  const tr = el("tr", table);
  tr.attrs = { "data-dash-incident": "INC-" + i, "data-asset": "a" + i, "data-host": "", "data-active": "true" };
  tr.getBoundingClientRect = () => {
    const top = BOX_TOP + HEAD + i * ROW_H - scroller.scrollTop;
    return { top, bottom: top + ROW_H, height: ROW_H };
  };
  const td = el("td", tr);
  const link = el("a", td);
  link.classes.add("inc-title");
  link.attrs["data-list-open"] = "";
  link.href = "http://h/incidents/INC-" + i;
  const box = el("input", td);
  tr.td = td; tr.link = link; tr.box = box;
  rows.push(tr);
}
table.querySelectorAll = () => rows;
const pane = el("aside", body);
const parts = {
  "[data-dash-graph-subject]": el("p", pane), "[data-dash-graph-empty]": el("p", pane),
  "[data-dash-graph-list]": el("div", pane), "[data-dash-graph-asset]": el("a", pane),
};
pane.querySelector = (s) => parts[s] || null;
const docListeners = {};
const navigated = [];
const windowScrolls = [];
globalThis.document = {
  activeElement: body,
  querySelector: (s) => (s === "[data-dash-graphs]" ? pane : s === "[data-dash-incident-table]" ? table : null),
  createElement: (tag) => el(tag), createElementNS: (_ns, tag) => el(tag),
  addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
};
globalThis.window = {
  setInterval: () => 1, clearInterval: () => {}, setTimeout, clearTimeout,
  location: { assign: (u) => navigated.push(u) },
  scrollTo: (...a) => windowScrolls.push(a), scrollBy: (...a) => windowScrolls.push(a),
};
const fetched = [];
globalThis.fetch = (url) => { fetched.push(url); return Promise.resolve({ ok: true, json: () => Promise.resolve({ tiles: [] }) }); };
eval(SRC);
const key = (k, target) => {
  const ev = { key: k, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  table.listeners.keydown.forEach((fn) => fn(ev));
  return ev.defaultPrevented;
};
const click = (target) => table.listeners.click.forEach((fn) => fn({ target }));
const down = (target) => (docListeners.mousedown || []).forEach((fn) => fn({ target }));
const sel = () => rows.findIndex((r) => r.classes.has("is-selected"));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const out = {};
  await wait(10);
  out.initial = { selected: sel(), fetched: fetched.slice() };
  out.held = [key("ArrowDown", rows[0]), key("ArrowDown", rows[1]), key("ArrowDown", rows[2])];
  out.afterHold = {
    selected: sel(), focused: rows.indexOf(document.activeElement),
    preventScroll: rows[3].focusOpts && rows[3].focusOpts.preventScroll,
    tabs: rows.map((r) => r.tabIndex), fetchedNow: fetched.length,
  };
  await wait(260);
  out.afterHold.fetched = fetched.slice();
  key("j", rows[3]);
  out.j = sel();
  key("k", rows[4]);
  out.k = sel();
  out.end = key("End", rows[3]);
  out.afterEnd = { selected: sel(), scrollTop: scroller.scrollTop, bottom: rows[N - 1].getBoundingClientRect().bottom };
  out.pastEnd = key("ArrowDown", rows[N - 1]);
  out.pastEndSelected = sel();
  out.home = key("Home", rows[N - 1]);
  out.afterHome = { selected: sel(), scrollTop: scroller.scrollTop };
  key("End", rows[0]);
  out.inInput = key("ArrowUp", rows[2].box);
  out.inInputSelected = sel();
  out.enterOnLink = key("Enter", rows[N - 1].link);
  out.navAfterLink = navigated.slice();
  out.enter = key("Enter", rows[N - 1]);
  out.navAfterEnter = navigated.slice();
  click(rows[1].td);
  out.click = { selected: sel(), focused: rows.indexOf(document.activeElement), navigated: navigated.length };
  click(rows[2].link);
  out.linkClickSelected = sel();
  down(table);
  out.downInside = rows.indexOf(document.activeElement);
  down(outside);
  out.downOutside = document.activeElement === body;
  out.windowScrolls = windowScrolls.length;
  console.log(JSON.stringify(out));
})();
"""


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    return node


def _run(harness: str, *args: str) -> dict:
    proc = subprocess.run([_node(), "-e", harness, "--", *args], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_js_keyboard_moves_selection_inside_list_only():
    out = _run(_KEYS_HARNESS, _js_fn("listPicker") + _js_fn("graphPane") + _js_block("bindDashGraphs"), "8")
    assert out["initial"] == {"selected": 0, "fetched": ["/api/v1/assets/a0/metrics"]}
    assert out["held"] == [True, True, True]
    hold = out["afterHold"]
    assert hold["selected"] == 3 and hold["focused"] == 3 and hold["preventScroll"] is True
    assert hold["tabs"] == [-1, -1, -1, 0, -1, -1, -1, -1]
    assert hold["fetchedNow"] == 1
    assert hold["fetched"] == ["/api/v1/assets/a0/metrics", "/api/v1/assets/a3/metrics"]
    assert out["j"] == 4 and out["k"] == 3
    assert out["end"] is True
    assert out["afterEnd"]["selected"] == 7
    assert out["afterEnd"]["scrollTop"] > 0 and out["afterEnd"]["bottom"] <= 300
    assert out["pastEnd"] is True and out["pastEndSelected"] == 7
    assert out["home"] is True and out["afterHome"] == {"selected": 0, "scrollTop": 0}
    assert out["inInput"] is False and out["inInputSelected"] == 7
    assert out["enterOnLink"] is False and out["navAfterLink"] == []
    assert out["enter"] is True and out["navAfterEnter"] == ["http://h/incidents/INC-7"]
    assert out["click"] == {"selected": 1, "focused": 1, "navigated": 1}
    assert out["linkClickSelected"] == 1
    assert out["downInside"] == 1
    assert out["downOutside"] is True
    assert out["windowScrolls"] == 0


def test_js_has_no_tab_trap():
    for block in (_js_block("bindDashGraphs"), _js_fn("graphPane"), _js_fn("listPicker"), _js_block("bindListPick")):
        assert '"Tab"' not in block and "'Tab'" not in block
        assert ".scrollIntoView(" not in block and "window.scroll" not in block


def test_pager_links_carry_no_top_jump_and_one_shared_helper():
    _add_many(12)
    client = _client()
    for path in ("/", "/incidents", "/assets", "/journal", "/ops", "/admin"):
        html = client.get(path).text
        for nav in re.findall(r'<nav class="pager".*?</nav>', html, flags=re.S):
            for href in re.findall(r'href="([^"]*)"', nav):
                assert "?" in href, (path, href)
                assert not href.startswith("#") and "#top" not in href, (path, href)
        assert html.count("data-pager-size-form") == html.count('class="pager-bar"'), path
    src = JS.read_text(encoding="utf-8")
    assert src.count("(function bindPagerScroll() {") == 1
    assert "bindPagerSize" not in src and ".form.submit()" not in src
    block = _js_block("bindPagerScroll")
    assert "sessionStorage" in block and 'url.hash = ""' in block
    for tpl in (ROOT / "frontend" / "templates").glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert "#top" not in text, tpl.name
        if tpl.name != "_pager.html":
            assert "pager_href(" not in text, tpl.name


_PAGER_HARNESS = r"""
const SRC = process.argv[1];
const PRESET = process.argv[2];
const LOC = new URL(process.argv[3]);
const store = {};
if (PRESET) store["forgesre-pager-scroll"] = PRESET.replace("NOW", String(Date.now()));
globalThis.sessionStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const assigned = [];
const scrolls = [];
const listeners = {};
const field = (name, value) => ({ name, value, disabled: false });
const sizeListeners = {};
const select = Object.assign(field("per_page", "50"), { addEventListener: (t, fn) => { sizeListeners[t] = fn; } });
const form = {
  elements: [field("status", "all"), select],
  getAttribute: (k) => (k === "action" ? "/incidents#incidents-list" : null),
  querySelector: () => select,
  addEventListener: (t, fn) => { listeners["form:" + t] = fn; },
};
globalThis.document = {
  readyState: "complete",
  documentElement: { scrollHeight: 3000 },
  querySelectorAll: (s) => (s === "form[data-pager-size-form]" ? [form] : []),
  addEventListener: (t, fn) => { listeners[t] = fn; },
};
globalThis.window = {
  scrollY: 640, innerHeight: 800,
  scrollTo: (x, y) => scrolls.push([x, y]),
  addEventListener: () => {},
  location: { pathname: LOC.pathname, search: LOC.search, href: LOC.href, assign: (u) => assigned.push(u) },
};
eval(SRC);
const out = { restored: scrolls.slice(), leftover: store["forgesre-pager-scroll"] || null };
const link = { href: "http://h/incidents?status=all&page=2#incidents-list" };
const ev = (extra) => Object.assign({
  button: 0, defaultPrevented: false,
  target: { closest: (s) => (s === ".pager-bar .pager a[href]" ? link : null) },
  preventDefault() { this.defaultPrevented = true; },
}, extra || {});
const ctrl = ev({ ctrlKey: true });
listeners.click(ctrl);
out.ctrlPrevented = ctrl.defaultPrevented;
const plain = ev();
listeners.click(plain);
out.plainPrevented = plain.defaultPrevented;
out.afterClick = assigned.slice();
out.savedClick = JSON.parse(store["forgesre-pager-scroll"]);
sizeListeners.change();
out.afterSize = assigned.slice();
out.savedSize = JSON.parse(store["forgesre-pager-scroll"]);
console.log(JSON.stringify(out));
"""


def test_js_pager_remembers_scroll_and_drops_fragment():
    block = _js_block("bindPagerScroll")
    out = _run(_PAGER_HARNESS, block, "", "http://h/incidents?status=all")
    assert out["restored"] == [] and out["leftover"] is None
    assert out["ctrlPrevented"] is False
    assert out["plainPrevented"] is True
    assert out["afterClick"] == ["http://h/incidents?status=all&page=2"]
    assert out["savedClick"]["to"] == "/incidents?status=all&page=2" and out["savedClick"]["y"] == 640
    assert out["afterSize"][-1] == "http://h/incidents?status=all&per_page=50"
    assert out["savedSize"]["to"] == "/incidents?status=all&per_page=50"


def test_js_pager_restores_only_matching_fresh_entry():
    block = _js_block("bindPagerScroll")
    fresh = json.dumps({"to": "/incidents?status=all&page=2", "y": 640, "at": "NOW"}).replace('"NOW"', "NOW")
    out = _run(_PAGER_HARNESS, block, fresh, "http://h/incidents?status=all&page=2")
    assert out["restored"] == [[0, 640]] and out["leftover"] is None
    other = _run(_PAGER_HARNESS, block, fresh, "http://h/history?page=2")
    assert other["restored"] == [] and other["leftover"] is None
    stale = json.dumps({"to": "/incidents?status=all&page=2", "y": 640, "at": int(time.time() * 1000) - 60000})
    old = _run(_PAGER_HARNESS, block, stale, "http://h/incidents?status=all&page=2")
    assert old["restored"] == []
    deep = json.dumps({"to": "/incidents?page=2", "y": 99999, "at": "NOW"}).replace('"NOW"', "NOW")
    clamped = _run(_PAGER_HARNESS, block, deep, "http://h/incidents?page=2")
    assert clamped["restored"] == [[0, 2200]]


def test_cache_bust_bumped():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v08-26" in base and "app.js?v=v08-26" in base
