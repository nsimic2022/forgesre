#!/usr/bin/env python3
"""Walk the ForgeSRE web UI like a signed-in user and report what is broken.

Follows every same-site <a href> (GET only, never submits a form), and checks that
every <form action>, <script src> and stylesheet points at something that exists.

  Live appliance:  python3 scripts/gui_crawl.py --base-url http://127.0.0.1:8080 \
                       --email admin@forgesre.local --password '...'
  In tests:        crawl(TestClient(app)) after signing in.

Exit code 1 when a page answers 5xx, a link answers 404, or a form posts to no route.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

# GETs that change state or leave the UI: never follow them.
SKIP_PREFIXES = ("/logout", "/api/", "/webhooks/", "/metrics", "/docs", "/redoc", "/openapi.json")
SKIP_SUFFIXES = ("/export", "/download", "/backup", "/run", "/delete", "/reset")
MAX_VARIANTS_PER_PATH = 3  # same path, different query strings
MAX_PAGES = 600


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.forms: list[tuple[str, str]] = []
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "form":
            self.forms.append(((a.get("method") or "get").lower(), a.get("action") or ""))
        elif tag == "script" and a.get("src"):
            self.assets.append(a["src"])
        elif tag == "link" and a.get("href") and "stylesheet" in (a.get("rel") or ""):
            self.assets.append(a["href"])


@dataclass
class Problem:
    kind: str  # page-error | broken-link | broken-form | broken-asset
    url: str
    status: int | str
    found_on: str

    def line(self) -> str:
        return f"{self.kind:13} {str(self.status):>4}  {self.url}  (from {self.found_on})"


@dataclass
class Report:
    pages: dict[str, int] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    form_targets: set[tuple[str, str]] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.problems


def _normalize(href: str, base: str) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    if "{{" in href or "{%" in href:
        return None
    parts = urlsplit(urljoin(base, href))
    if parts.scheme not in ("", "http", "https") or (parts.netloc and parts.netloc != urlsplit(base).netloc):
        return None
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return parts.path + (f"?{query}" if query else "")


def _skip(path: str) -> bool:
    bare = path.split("?", 1)[0]
    return bare.startswith(SKIP_PREFIXES) or bare.endswith(SKIP_SUFFIXES) or bare.startswith("/static/")


def _route_matcher(routes: Iterable[tuple[str, str]] | None):
    """(method, path-pattern) pairs, e.g. ('POST', '/assets/{asset_id}/edit')."""
    if routes is None:
        return None
    compiled = []
    for method, pattern in routes:
        rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(pattern)) + "$"
        compiled.append((method.upper(), re.compile(rx)))

    def has(method: str, path: str) -> bool:
        return any(m == method.upper() and rx.match(path) for m, rx in compiled)

    return has


def crawl(client: Any, start: str = "/", routes: Iterable[tuple[str, str]] | None = None,
          base: str = "http://testserver") -> Report:
    """``client`` needs ``get(url, follow_redirects=...)`` returning ``.status_code``, ``.text``,
    ``.headers``. ``routes`` lets broken form actions be detected without submitting them."""
    has_route = _route_matcher(routes)
    report = Report()
    seen: set[str] = set()
    variants: dict[str, int] = {}
    assets_checked: dict[str, int] = {}
    queue: deque[tuple[str, str]] = deque([(start, "(start)")])
    while queue and len(report.pages) < MAX_PAGES:
        url, found_on = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        bare = url.split("?", 1)[0]
        variants[bare] = variants.get(bare, 0) + 1
        if "?" in url and variants[bare] > MAX_VARIANTS_PER_PATH:
            continue
        try:
            resp = client.get(url, follow_redirects=True)
            status: int | str = resp.status_code
        except Exception as exc:  # an exception inside the app is a 500 to the user
            report.pages[url] = 500
            report.problems.append(Problem("page-error", url, type(exc).__name__, found_on))
            continue
        report.pages[url] = int(status)
        if int(status) >= 500:
            report.problems.append(Problem("page-error", url, status, found_on))
            continue
        if int(status) == 404:
            report.problems.append(Problem("broken-link", url, status, found_on))
            continue
        if "text/html" not in resp.headers.get("content-type", ""):
            continue
        parser = _Links()
        try:
            parser.feed(resp.text)
        except Exception:
            continue
        here = base + url
        for href in parser.links:
            target = _normalize(href, here)
            if target and not _skip(target) and target not in seen:
                queue.append((target, url))
        for method, action in parser.forms:
            target = _normalize(action, here) if action else url
            if not target:
                continue
            if method not in ("get", "post"):
                continue  # method="dialog" closes a <dialog>; it never reaches the server
            path = target.split("?", 1)[0]
            report.form_targets.add((method.upper(), path))
            if has_route and not has_route(method, path):
                report.problems.append(Problem("broken-form", f"{method.upper()} {path}", "no route", url))
        for src in parser.assets:
            target = _normalize(src, here)
            if not target:
                continue
            if target not in assets_checked:
                try:
                    assets_checked[target] = client.get(target).status_code
                except Exception:
                    assets_checked[target] = 500
            if assets_checked[target] >= 400:
                report.problems.append(Problem("broken-asset", target, assets_checked[target], url))
    # One line per problem target.
    unique: dict[tuple[str, str], Problem] = {}
    for p in report.problems:
        unique.setdefault((p.kind, p.url), p)
    report.problems = list(unique.values())
    return report


def get_routes(app: Any) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, including routes of included routers
    (FastAPI >= 0.120 keeps those behind an include wrapper instead of copying them)."""
    out: list[tuple[str, str]] = []

    def walk(routes: Iterable[Any], prefix: str) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                ctx = getattr(route, "include_context", None)
                walk(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
                continue
            path = getattr(route, "path", None)
            for method in getattr(route, "methods", None) or ():
                if path is not None:
                    out.append((method, prefix + path))

    walk(getattr(app, "routes", []), "")
    return out


def unlinked_pages(app: Any, report: Report) -> list[str]:
    """GET pages with no path parameter that no crawled page links to."""
    reached = {u.split("?", 1)[0] for u in report.pages}
    out = []
    for method, path in get_routes(app):
        if method != "GET" or "{" in path or _skip(path) or path in reached:
            continue
        out.append(path)
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args(argv)
    import httpx

    base = args.base_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=30, verify=False) as client:
        login = client.post("/login", data={"email": args.email, "password": args.password})
        if "forgesre_session" not in client.cookies:
            print(f"sign-in failed (HTTP {login.status_code})", file=sys.stderr)
            return 2
        report = crawl(client, base=base)
    print(f"{len(report.pages)} pages visited, {len(report.problems)} problems")
    for p in sorted(report.problems, key=lambda p: (p.kind, p.url)):
        print("  " + p.line())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
