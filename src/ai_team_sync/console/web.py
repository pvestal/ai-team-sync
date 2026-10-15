"""Loopback-only browser view of claim verification (#3589).

GET-only. Binds 127.0.0.1 unless told otherwise; the data it shows comes from
read-only collectors and it exposes no action that changes anything.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable

from .evidence import EvidenceConfig
from .verify import ticket_report

_TICKET_PATH = re.compile(r"^/api/ticket/(\d{1,7})$")
_CACHE_SECONDS = 60


def _page() -> bytes:
    return resources.files("ai_team_sync.console").joinpath("verify_page.html").read_bytes()


class _ReportCache:
    def __init__(self, build: Callable[[int], dict[str, Any]]):
        self.build = build
        self.lock = threading.Lock()
        self.items: dict[int, tuple[float, dict[str, Any]]] = {}

    def get(self, ticket_id: int, refresh: bool) -> dict[str, Any]:
        with self.lock:
            cached = self.items.get(ticket_id)
            if cached and not refresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
                return cached[1]
        report = self.build(ticket_id)
        with self.lock:
            self.items[ticket_id] = (time.monotonic(), report)
        return report


def make_handler(cache: _ReportCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tower-console-verify"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                self._send(200, _page(), "text/html; charset=utf-8")
                return
            match = _TICKET_PATH.match(path)
            if match:
                try:
                    report = cache.get(int(match.group(1)), refresh="refresh=1" in query)
                    body = json.dumps(report, default=str).encode()
                    self._send(200, body, "application/json")
                except Exception as exc:  # noqa: BLE001 - surface the failure, never a blank page
                    error = {"error": type(exc).__name__, "detail": str(exc)[:300]}
                    self._send(500, json.dumps(error).encode(), "application/json")
                return
            self._send(404, b'{"error":"not found"}', "application/json")

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, config: EvidenceConfig | None = None) -> None:
    cfg = config or EvidenceConfig.from_environment()
    cache = _ReportCache(lambda ticket: ticket_report(ticket, cfg))
    server = ThreadingHTTPServer((host, port), make_handler(cache))
    print(f"claim verification: http://{host}:{server.server_address[1]}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
