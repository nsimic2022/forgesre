#!/usr/bin/env python3
"""Take the documentation screenshots from a running ForgeSRE.

Signs in, opens each page of the new UI and saves PNGs under docs/images/ (ui-*.png), so the
docs can be refreshed after a UI change instead of going stale. Needs Playwright and a
Chromium/Chrome (``pip install playwright && playwright install chromium``); it is a
maintainer tool, not part of the appliance.

    python3 scripts/screenshots.py --base http://127.0.0.1:8080 --email admin@x --password ...

Use a demo or lab install: the screenshots show whatever data that install has.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT, MAX_HEIGHT = 1440, 900, 2000

# (file stem, path, optional CSS selector of a button or tab to click first)
PAGES = [
    ("noc", "/app/", None),
    ("tv", "/app/tv", None),
    ("incidents", "/app/incidents", None),
    ("incident", "{incident}", None),
    ("oncall", "/app/incidents/on-call", None),
    ("inventory", "/app/inventory", None),
    ("dependency-map", "/app/inventory/map", None),
    ("automation-filters", "/app/automation", None),
    ("automation-rules", "/app/automation", "role=tab[name='Routing rules']"),
    ("automation-escalation", "/app/automation", "role=tab[name='Escalation']"),
    ("investigations", "/app/investigations", None),
    ("reports", "/app/reports", None),
    ("inventory-add", "/app/inventory/add", None),
    ("settings", "/app/settings", None),
    ("settings-integrations", "/app/settings", "role=button[name='Integrations']"),
    ("settings-messages", "/app/settings", "role=button[name='Messages and templates']"),
    ("settings-appearance", "/app/settings", "role=button[name='Appearance']"),
]


def main() -> None:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("FORGESRE_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--email", default=os.environ.get("FORGESRE_EMAIL", ""))
    ap.add_argument("--password", default=os.environ.get("FORGESRE_PASSWORD", ""))
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--chrome", default=os.environ.get("CHROME", ""), help="path to a Chrome/Chromium binary (optional)")
    ap.add_argument("--only", nargs="*", help="file stems to take (default: all)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        if args.chrome:
            launch["executable_path"] = args.chrome
        browser = p.chromium.launch(**launch)
        page = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}).new_page()
        page.goto(f"{args.base}/login")
        page.fill("input[name=email]", args.email)
        page.fill("input[name=password]", args.password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        noc = json.loads(page.evaluate("fetch('/api/v1/noc').then(r => r.text())"))
        active = (noc.get("incidents") or {}).get("active") or (noc.get("incidents") or {}).get("acknowledged") or []
        incident = f"/app/incidents/{active[0]['number']}" if active else "/app/incidents"
        for stem, path, click in PAGES:
            if args.only and stem not in args.only:
                continue
            page.goto(args.base + path.format(incident=incident))
            page.wait_for_load_state("networkidle")
            if click:
                page.locator(click).first.click()
            page.wait_for_timeout(1200)
            height = min(MAX_HEIGHT, page.evaluate("document.documentElement.scrollHeight"))
            page.set_viewport_size({"width": WIDTH, "height": max(HEIGHT, height)})
            target = out / f"ui-{stem}.png"
            page.screenshot(path=str(target), clip={"x": 0, "y": 0, "width": WIDTH, "height": max(HEIGHT, height)})
            page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
            print(f"wrote {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
        browser.close()


if __name__ == "__main__":
    main()
