"""A local stand-in for Echo Brain that the whole test run points at (#3537).

Tests used to inherit the production default ECHO_BRAIN_URL=localhost:8309, and
every session they completed -- in-process or in a spawned ats-server -- was
posted to the live Echo Brain. conftest starts one Tripwire per test process and
sets ECHO_BRAIN_URL to it before anything is imported, so every child process
inherits it too. Nothing a test starts can reach the real service.

The tripwire answers 503 to everything (so nothing believes Echo accepted it) and
records each request, so a completion event that arrives here is a test failure,
not a silent drop.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMPLETION_PATH = "/api/ats/session-completed"


class Tripwire:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        tripwire = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                with tripwire._lock:
                    tripwire.requests.append(
                        {"method": self.command, "path": self.path, "body": body})
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _record

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def completion_events(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [r for r in self.requests[since:]
                    if r["path"].split("?")[0] == COMPLETION_PATH]

    @staticmethod
    def describe(requests: list[dict]) -> str:
        out = []
        for r in requests[:5]:
            try:
                sid = json.loads(r["body"]).get("session_id")
            except (ValueError, AttributeError):
                sid = None
            out.append(f"{r['method']} {r['path']} session={sid}")
        return "; ".join(out) + (" ..." if len(requests) > 5 else "")
