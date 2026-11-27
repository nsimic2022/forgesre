#!/usr/bin/env python3
"""Walk the ForgeSRE UI like a signed-in user and report what is broken.

The UI is the React app at /app, which reads everything from /api/v1. The walk:

1. opens every page of the app (they must answer 200 once the app is built);
2. calls every GET endpoint of /api/v1 that needs no body, filling ids from what the lists return
   (an incident number, an asset id, a skill), and reports any 5xx;
3. checks every /api/v1 path the app's source calls exists on the server (frontend and backend in
   step);
4. checks the old classic URLs still lead into the app.

  Live appliance:  python3 scripts/gui_crawl.py --base-url http://127.0.0.1:8080 \
                       --email admin@forgesre.local --password '...'
  In tests:        crawl(TestClient(app), routes=get_routes(app)) after signing in.

Exit code 1 when anything answers 5xx, a page is missing, or the app calls a path the server lacks.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
APP_PAGES = ["/app/", "/app/tv", "/app/incidents", "/app/incidents/on-call", "/app/incidents/{number}", "/app/inventory",
             "/app/inventory/map", "/app/inventory/add", "/app/automation", "/app/investigations", "/app/forgeai", "/app/reports", "/app/settings",
             "/app/settings/flows", "/app/settings/integrations/zabbix"]
OLD_URLS = ["/", "/classic", "/incidents", "/assets", "/discovery", "/playrules", "/playbooks", "/escalation", "/ops",
            "/journal", "/health-ui", "/admin", "/investigations", "/forgeai", "/history"]
# GETs that download, stream, change state or leave the UI.
SKIP = ("/download", "/export", ".csv", "/template", "/stream", "/events", "/mcp", "/webhooks", "/sd/", "/metrics")


@dataclass
class Problem:
    kind: str
    url: str
    status: int | None = None
    found_on: str = ""

    def line(self) -> str:
        return f"{self.kind:<14} {self.status or '':<4} {self.url}" + (f"  ({self.found_on})" if self.found_on else "")


@dataclass
class Report:
    pages: dict[str, int] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


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


def _json(resp: Any) -> Any:
    try:
        return resp.json() if hasattr(resp, "json") else __import__("json").loads(resp.text)
    except Exception:
        return None


def _ids(client: Any) -> dict[str, str]:
    """Real ids to fill path parameters: first incident, asset and ForgeAI skill."""
    ids: dict[str, str] = {}
    page = _json(client.get("/api/v1/incidents-page?limit=1"))
    rows = (page or {}).get("rows") or (page or {}).get("incidents") or []
    if rows:
        ids["number"] = str(rows[0].get("number") or "")
    inv = _json(client.get("/api/v1/inventory?limit=1"))
    if (inv or {}).get("rows"):
        ids["asset_id"] = str(inv["rows"][0].get("asset_id") or "")
    skills = _json(client.get("/api/v1/forgeai/skills"))
    if (skills or {}).get("skills"):
        ids["slug"] = str(skills["skills"][0].get("slug") or "")
    return {k: v for k, v in ids.items() if v}


def _fill(path: str, ids: dict[str, str]) -> str | None:
    names = re.findall(r"{([^}:]+)(?::[^}]*)?}", path)
    if any(n not in ids for n in names):
        return None
    for n in names:
        path = re.sub(r"{" + n + r"(?::[^}]*)?}", ids[n], path)
    return path


def frontend_api_paths(src: Path = ROOT / "web" / "src") -> set[str]:
    """Every /api/v1 path the app's source calls, ${…} replaced by {param}."""
    found: set[str] = set()
    for file in src.rglob("*.ts*"):
        if ".test." in file.name:
            continue
        for raw in re.findall(r"[\"'`](/api/v1/[^\"'`?#\s]*)", file.read_text(encoding="utf-8")):
            raw = re.sub(r"\$\{[^}]*$", "", raw)  # a ${…} that holds a query string, cut where it starts
            path = re.sub(r"\$\{[^}]*\}", "{param}", raw).rstrip("/")
            found.add(path)
    return found


def _known(path: str, routes: Iterable[tuple[str, str]]) -> bool:
    """True when some route has the same segments, a {param} on either side matching anything."""
    want = path.strip("/").split("/")
    for _method, route in routes:
        have = route.strip("/").split("/")
        if len(have) == len(want) and all(a == b or a.startswith("{") or b.startswith("{") for a, b in zip(want, have)):
            return True
    return False


def crawl(client: Any, routes: Iterable[tuple[str, str]] | None = None, **_ignored: Any) -> Report:
    """Run the walk with a signed-in client (TestClient or StdlibClient)."""
    report = Report()
    routes = list(routes or [])
    ids = _ids(client)
    # 1. pages of the app
    first = client.get("/app/")
    built = first.status_code == 200
    for page in APP_PAGES:
        url = _fill(page, ids)
        if url is None:
            continue
        status = client.get(url).status_code
        report.pages[url] = status
        if built and status != 200:
            report.problems.append(Problem("page", url, status))
    # 2. every GET read of the API
    for method, path in routes:
        if method != "GET" or not path.startswith("/api/v1/") or any(x in path for x in SKIP):
            continue
        url = _fill(path, ids)
        if url is None:
            continue
        try:
            status = client.get(url).status_code
        except Exception as exc:  # a crash inside the app is a 5xx
            report.problems.append(Problem("crash", url, None, str(exc)[:120]))
            continue
        report.pages[url] = status
        if status >= 500:
            report.problems.append(Problem("server-error", url, status))
    # 3. the app only calls paths the server has
    if routes:
        for path in sorted(frontend_api_paths()):
            if not _known(path, routes):
                report.problems.append(Problem("missing-route", path, None, "called by web/src"))
    # 4. old classic addresses lead into the app
    for old in OLD_URLS:
        resp = client.get(old, follow_redirects=False)
        target = (getattr(resp, "headers", {}) or {}).get("location", "")
        report.pages[old] = resp.status_code
        if resp.status_code not in (301, 302, 303, 307, 308) or not (target.startswith("/app") or target.startswith("/login")):
            report.problems.append(Problem("old-url", old, resp.status_code, target))
    return report


class _Response:
    def __init__(self, status_code: int, text: str, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers


class StdlibClient:
    """Tiny cookie-keeping HTTP client on the standard library, so the script runs on a
    bare appliance host (httpx lives only inside the Core container)."""

    def __init__(self, base: str, timeout: float = 30) -> None:
        import http.cookiejar
        import ssl
        import urllib.request

        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # appliances often use a self-signed certificate

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return None

        handlers = [urllib.request.HTTPCookieProcessor(self.cookies), urllib.request.HTTPSHandler(context=ctx)]
        self._follow = urllib.request.build_opener(*handlers)
        self._stay = urllib.request.build_opener(*handlers, _NoRedirect())

    def _open(self, req, follow: bool) -> _Response:
        import urllib.error

        opener = self._follow if follow else self._stay
        try:
            resp = opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as err:
            resp = err
        body = resp.read() or b""
        headers = {k.lower(): v for k, v in resp.headers.items()}
        charset = resp.headers.get_content_charset() or "utf-8"
        return _Response(resp.status if hasattr(resp, "status") else resp.code, body.decode(charset, "replace"), headers)

    def get(self, url: str, follow_redirects: bool = True) -> _Response:
        import urllib.request

        return self._open(urllib.request.Request(self.base + url), follow_redirects)

    def post_form(self, url: str, data: dict[str, str]) -> _Response:
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode(data).encode()
        return self._open(urllib.request.Request(self.base + url, data=body, method="POST"), False)

    def has_cookie(self, name: str) -> bool:
        return any(c.name == name for c in self.cookies)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args(argv)

    base = args.base_url.rstrip("/")
    client = StdlibClient(base)
    try:
        login = client.post_form("/login", {"email": args.email, "password": args.password})
    except OSError as exc:
        print(f"cannot reach {base}: {exc}", file=sys.stderr)
        return 2
    if not client.has_cookie("forgesre_session"):
        print(f"sign-in failed (HTTP {login.status_code}): check the email and password", file=sys.stderr)
        return 2
    report = crawl(client)
    print(f"{len(report.pages)} pages and API reads visited, {len(report.problems)} problems")
    for p in sorted(report.problems, key=lambda p: (p.kind, p.url)):
        print("  " + p.line())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
