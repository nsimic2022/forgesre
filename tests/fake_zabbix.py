"""A tiny fake Zabbix JSON-RPC server for tests of the Flow page (no real Zabbix needed)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def answers(failed_send: bool = True) -> dict[str, Any]:
    now = int(time.time())
    sent = [{"clock": str(now - 3600), "status": "1", "error": "", "retries": "0", "subject": "Problem: Disk space low"}]
    if failed_send:
        sent.insert(0, {"clock": str(now - 60), "status": "2", "error": "Couldn't connect to server", "retries": "3", "subject": "Problem: High CPU"})
    return {
        "apiinfo.version": "7.0.5",
        "host.get": "214",
        "hostgroup.get": [{"name": "Linux servers"}, {"name": "Network"}],
        "problem.get": "2",
        "mediatype.get": [{"mediatypeid": "40", "name": "ForgeSRE", "type": "4", "status": "0"}],
        "action.get": [{"name": "Send to ForgeSRE", "status": "0"}],
        "alert.get": sent,
    }


def start(failed_send: bool = True, refuse: set[str] | None = None) -> tuple[ThreadingHTTPServer, str, list[dict[str, Any]]]:
    """Start on a free port; returns (server, url, calls). ``refuse`` methods answer "No permissions"."""
    calls: list[dict[str, Any]] = []
    table = answers(failed_send)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server API
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            calls.append({"method": body.get("method"), "auth": self.headers.get("Authorization", "")})
            method = body.get("method")
            if refuse and method in refuse:
                out = {"jsonrpc": "2.0", "error": {"code": -32500, "message": "Application error.", "data": "No permissions to call \"%s\"." % method}, "id": body.get("id")}
            else:
                out = {"jsonrpc": "2.0", "result": table.get(method, []), "id": body.get("id")}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/zabbix", calls


if __name__ == "__main__":
    srv, url, _ = start()
    print(url, flush=True)
    threading.Event().wait()
