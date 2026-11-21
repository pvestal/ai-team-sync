"""Read-only ATS MCP over Streamable HTTP, for remote clients (#3760).

A remote client (claude.ai chat, including voice mode) may READ ATS context --
task briefs, preflight, decisions, active work -- and nothing else. This is a
separate server from the stdio ``ats-mcp``, deliberately sharing none of its
dispatch, because that dispatch carries session identity: it resolves the
caller from pointer files and keyrings, heartbeats sessions it holds, and
hosts every mutating tool. Reusing it would make "read-only" depend on every
branch of a 3,500-line function staying correct. Here read-only is structural,
in three independent layers:

1. TOOL ALLOWLIST. ``READONLY_TOOLS`` is the whole catalog; ``call_tool``
   refuses any other name before doing anything. No tool takes or resolves a
   session identity, and no capability header is ever sent.
2. UPSTREAM ALLOWLIST. Every outbound HTTP request goes through
   ``_GuardedTransport``, which refuses any (method, path) not in
   ``UPSTREAM_ALLOWLIST``. Even a future bug in a handler cannot reach a
   mutating ATS route (POST /api/sessions, /api/locks, /api/decisions, ...).
3. BEARER GATE. ``BearerGate`` is the outermost ASGI layer. Every HTTP request
   without ``Authorization: Bearer <secret>`` gets 401 before the MCP layer,
   and therefore before ATS, sees a single byte of it.

The listener binds loopback only (refuses anything else); public reach is a
Tailscale Funnel in front of it. See docs/remote-readonly-mcp.md.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, Tool

ATS_URL = os.environ.get("ATS_SERVER_URL", "http://127.0.0.1:8400").rstrip("/")
ECHO_URL = os.environ.get("ECHO_BRAIN_URL", "http://127.0.0.1:8309").rstrip("/")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8402
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ai-team-sync" / "remote-mcp.token"
MIN_TOKEN_LEN = 32
# A JSON-RPC tool call is a few hundred bytes. Anything near this is not a
# legitimate request; refusing it before reading bounds memory per request.
MAX_BODY_BYTES = 64 * 1024
# Bounds for an internet-facing endpoint (#3760 review r1). An upstream answer
# larger than MAX_UPSTREAM_BYTES is refused while streaming, never buffered;
# tool text is truncated to MAX_OUTPUT_CHARS; uvicorn answers 503 past
# MAX_CONCURRENCY open connections/requests.
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 60_000
MAX_CONCURRENCY = 16
REMOTE_WORKER_LABEL = "remote-readonly"

# The names a remote caller may invoke. Kept as a literal, not derived from the
# stdio catalog, so adding a tool to ats-mcp can never widen this surface.
READONLY_TOOL_NAMES = frozenset({
    "task_brief",
    "preflight",
    "get_decision_history",
    "team_status",
    "get_session_details",
    "list_all_locks",
    "ats_version",
})

# Every (method, base, path regex) this server may send upstream. GETs are reads
# by HTTP contract on ATS; the two POSTs are the read-only brief builder (a
# brief never binds a ticket or claims anything, routers/briefs.py) and Echo's
# advisory preflight.
_SID = r"[A-Za-z0-9-]{1,64}"
UPSTREAM_ALLOWLIST: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("POST", "ats", re.compile(r"/api/brief")),
    ("GET", "ats", re.compile(r"/api/sessions")),
    ("GET", "ats", re.compile(rf"/api/sessions/{_SID}")),
    ("GET", "ats", re.compile(r"/api/decisions")),
    ("GET", "ats", re.compile(r"/api/locks")),
    ("GET", "ats", re.compile(r"/api/restarts")),
    ("GET", "ats", re.compile(r"/api/version")),
    ("POST", "echo", re.compile(r"/api/preflight")),
)


class UpstreamRefused(RuntimeError):
    """An outbound request outside UPSTREAM_ALLOWLIST was attempted."""


class UpstreamTooLarge(httpx.HTTPError):
    """An allowed upstream answered more than MAX_UPSTREAM_BYTES."""

    def __init__(self, message: str):
        super().__init__(message)


def _origin(url: str) -> tuple[str, str, int | None]:
    u = httpx.URL(url)
    return (u.scheme, u.host, u.port)


def upstream_allowed(method: str, url: httpx.URL) -> bool:
    origin = (url.scheme, url.host, url.port)
    bases = {"ats": _origin(ATS_URL), "echo": _origin(ECHO_URL)}
    for m, base, pattern in UPSTREAM_ALLOWLIST:
        if m == method.upper() and origin == bases[base] and pattern.fullmatch(url.path):
            return True
    return False


class _GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not upstream_allowed(request.method, request.url):
            raise UpstreamRefused(f"{request.method} {request.url} is not a read-only upstream")
        response = await self._inner.handle_async_request(request)
        # Read the body here, bounded, so an oversized upstream answer is
        # refused while streaming instead of being buffered whole.
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in response.stream:  # raw transport stream
                size += len(chunk)
                if size > MAX_UPSTREAM_BYTES:
                    raise UpstreamTooLarge(
                        f"{request.method} {request.url.path} answered more than "
                        f"{MAX_UPSTREAM_BYTES} bytes")
                chunks.append(chunk)
        finally:
            await response.stream.aclose()
        return httpx.Response(response.status_code, headers=response.headers,
                              content=b"".join(chunks), request=request,
                              extensions=response.extensions)

    async def aclose(self) -> None:
        await self._inner.aclose()


# Tests replace this to observe or stub upstream traffic; production never does.
_inner_transport: httpx.AsyncBaseTransport | None = None


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    # follow_redirects stays False: a redirect is a new request the allowlist
    # would still check, but there is no reason to follow one at all.
    return httpx.AsyncClient(transport=_GuardedTransport(_inner_transport),
                             timeout=timeout, follow_redirects=False)


def _text(s: str) -> list[TextContent]:
    if len(s) > MAX_OUTPUT_CHARS:
        s = s[:MAX_OUTPUT_CHARS] + f"\n[truncated at {MAX_OUTPUT_CHARS} characters]"
    return [TextContent(type="text", text=s)]


READONLY_TOOLS: list[Tool] = [
    Tool(
        name="task_brief",
        description=(
            "Read-only. The ATS context packet for a piece of work: live blockers, "
            "prior decisions, prior work, the ticket's current operator rulings, "
            "each line with provenance and a citation. Reading it claims nothing."),
        inputSchema={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The work, in a sentence."},
                "task_id": {"type": "integer", "minimum": 1,
                            "description": "Tower task id for exact rulings."},
                "repo_root": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "recall": {"type": "boolean"},
                "resolve_task": {"type": "boolean"},
            },
            "required": ["objective"],
        },
    ),
    Tool(
        name="preflight",
        description=(
            "Read-only advisory. Has this action been tried before, and what "
            "happened? Returns CLEAR / CAUTION / STRONG_WARNING / "
            "INSUFFICIENT_EVIDENCE with citations. Changes nothing."),
        inputSchema={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "task_id": {"type": "integer", "minimum": 1},
                "operation_type": {"type": "string",
                                   "enum": ["investigate", "edit", "test", "render",
                                            "gpu_canary", "service_restart",
                                            "migration", "other"]},
                "repo_root": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "entities": {"type": "object"},
            },
            "required": ["objective"],
        },
    ),
    Tool(
        name="get_decision_history",
        description=("Read-only. Decisions logged in ATS, newest first, optionally "
                     "filtered by ticket, repo or start time."),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "repo_root": {"type": "string"},
                "since": {"type": "string", "format": "date-time"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
            },
        },
    ),
    Tool(
        name="team_status",
        description="Read-only. Active agent sessions: who, ticket, scope, idle time.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_session_details",
        description="Read-only. One ATS session's record by id.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="list_all_locks",
        description="Read-only. Active scope locks and their holders.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="ats_version",
        description="Read-only. Deployed ATS build identity.",
        inputSchema={"type": "object", "properties": {}},
    ),
]

assert {t.name for t in READONLY_TOOLS} == READONLY_TOOL_NAMES


async def _task_brief(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    body: dict[str, Any] = {"objective": str(a["objective"]),
                            "repo_root": str(a.get("repo_root") or ""),
                            "scope": list(a.get("scope") or []),
                            "recall": bool(a.get("recall", True)),
                            "resolve_task": bool(a.get("resolve_task", True)),
                            # No caller session: a remote reader is nobody's session.
                            "session_id": ""}
    if a.get("task_id"):
        body["task_id"] = int(a["task_id"])
    r = await c.post(f"{ATS_URL}/api/brief", json=body)
    if r.status_code >= 400:
        return _text(f"Task brief refused (HTTP {r.status_code}): {r.text[:500]}")
    return _text(r.json().get("rendered") or json.dumps(r.json(), indent=1)[:20000])


async def _preflight(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    payload = {"objective": str(a["objective"]),
               "requesting_worker": REMOTE_WORKER_LABEL,
               "requesting_session": None,
               "repo_root": str(a.get("repo_root") or ""),
               "tower_task_id": a.get("task_id"),
               "operation_type": a.get("operation_type", "other"),
               "declared_scope": list(a.get("scope") or []),
               "entities": dict(a.get("entities") or {}),
               # Echo persists preflight_requests/preflight_evidence unless told
               # read_only (preflight_api.py); a remote reader writes nothing.
               "read_only": True}
    try:
        r = await c.post(f"{ECHO_URL}/api/preflight", json=payload, timeout=90)
        r.raise_for_status()
        d = r.json()
    except UpstreamRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        return _text(f"PREFLIGHT UNAVAILABLE ({type(exc).__name__}). This is NOT a "
                     "CLEAR result: no history was checked.")
    lines = [f"{d.get('disposition')} (read-only preflight; not recorded)",
             "", str(d.get("recommended_next") or "")]
    ts = d.get("task_state") or {}
    if ts:
        lines += ["", f"TOWER TASK #{ts.get('task_id')} {ts.get('task_key') or ''}".rstrip(),
                  f"  status: {ts.get('status')}  gate: {ts.get('gate')}  "
                  f"priority: {ts.get('priority')}"]
        if ts.get("recommendation"):
            lines.append(f"  ruling: {str(ts['recommendation'])[:300]}")
    for label, key in (("OPERATOR RULINGS", "operator_decisions"),
                       ("FAILED APPROACHES", "failed_approaches"),
                       ("CHANGED SINCE", "change_since_evidence")):
        items = d.get(key) or []
        if items:
            lines += ["", label]
            for e in items[:4]:
                lines.append(f"  [{e.get('authority_class')}] {e.get('citation')}")
                if e.get("evidence_text"):
                    lines.append(f"    {str(e['evidence_text'])[:200]}")
    if d.get("degraded_reason"):
        lines += ["", f"degraded: {d['degraded_reason']}"]
    lines += ["", "Advisory. Nothing was changed."]
    return _text("\n".join(lines))


async def _decisions(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    params: dict[str, Any] = {"limit": max(1, min(int(a.get("limit") or 30), 200))}
    if a.get("ticket_id"):
        params["ticket_id"] = int(a["ticket_id"])
    elif a.get("repo_root"):
        params["repo_root"] = str(a["repo_root"])
    if a.get("since"):
        params["since"] = str(a["since"])
    r = await c.get(f"{ATS_URL}/api/decisions", params=params)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return _text("No decisions match.")
    out = [f"{len(rows)} decision(s):", ""]
    for d in rows:
        out.append(f"{d.get('title')}")
        if d.get("ticket_id") is not None:
            out.append(f"  ticket: #{d['ticket_id']}")
        out.append(f"  chose: {d.get('chosen')}")
        if d.get("rejected"):
            out.append(f"  rejected: {d['rejected']}")
        out.append(f"  reasoning: {d.get('reasoning')}")
        out.append(f"  session {d.get('session_id')} at {d.get('created_at')}")
        out.append("")
    return _text("\n".join(out))


async def _team_status(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    r = await c.get(f"{ATS_URL}/api/sessions", params={"status": "active"})
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return _text("No active sessions.")
    out = [f"{len(rows)} active session(s):", ""]
    for s in rows:
        idle = s.get("idle_seconds")
        idle_txt = f"{int(idle // 60)}m" if isinstance(idle, (int, float)) else "?"
        stale = " STALE" if s.get("is_stale") else ""
        ticket = f" ticket #{s['ticket_id']}" if s.get("ticket_id") is not None else ""
        out.append(f"- {s.get('agent')} session {s.get('id')}{ticket} (idle {idle_txt}){stale}")
        out.append(f"  {s.get('description')}")
        if s.get("scope"):
            out.append(f"  scope: {', '.join(s['scope'])}")
        out.append("")
    return _text("\n".join(out))


async def _session_details(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    sid = str(a.get("session_id") or "")
    if not re.fullmatch(_SID, sid):
        return _text("session_id must be an ATS session id.")
    r = await c.get(f"{ATS_URL}/api/sessions/{sid}")
    if r.status_code == 404:
        return _text(f"No session {sid}.")
    r.raise_for_status()
    d = r.json()
    keys = ("id", "agent", "developer", "status", "ticket_id", "branch", "repo_root",
            "description", "scope", "lock_count", "decision_count", "started_at",
            "summary")
    return _text("\n".join(f"{k}: {d.get(k)}" for k in keys if d.get(k) not in (None, "", [])))


async def _locks(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    r = await c.get(f"{ATS_URL}/api/locks")
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return _text("No active locks.")
    return _text("\n".join(
        f"{l.get('pattern')} ({l.get('mode')}) held by {l.get('agent')} "
        f"session {l.get('session_id')}, expires {l.get('expires_at')}" for l in rows))


async def _version(c: httpx.AsyncClient, a: dict[str, Any]) -> list[TextContent]:
    from ai_team_sync.build_info import identity, summary_line
    lines = [summary_line(identity("ats-mcp-remote"))]
    try:
        r = await c.get(f"{ATS_URL}/api/version", timeout=5)
        lines.append(summary_line(r.json()) if r.status_code == 200 else
                     f"ats-rest: HTTP {r.status_code}")
    except UpstreamRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ats-rest: unreachable ({type(exc).__name__})")
    return _text("\n".join(lines))


_HANDLERS = {
    "task_brief": _task_brief,
    "preflight": _preflight,
    "get_decision_history": _decisions,
    "team_status": _team_status,
    "get_session_details": _session_details,
    "list_all_locks": _locks,
    "ats_version": _version,
}
assert set(_HANDLERS) == READONLY_TOOL_NAMES


def build_server() -> Server:
    server: Server = Server("ats-remote-readonly")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return list(READONLY_TOOLS)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        # Raising makes the MCP layer answer isError=true, so a refusal can never
        # be read as a successful result.
        if name not in READONLY_TOOL_NAMES:
            raise PermissionError(f"Refused: {name!r} is not available on the "
                                  "read-only remote surface.")
        async with _client() as c:
            try:
                return await _HANDLERS[name](c, arguments or {})
            except UpstreamRefused as exc:
                raise PermissionError(f"Refused: {exc}") from None
            except httpx.HTTPError as exc:
                raise RuntimeError(f"ATS read failed: {type(exc).__name__}") from None

    return server


# ---- bearer gate -------------------------------------------------------------

def load_token(path: Path) -> str:
    """Read the shared secret, refusing a missing, weak, or world/group-readable one."""
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"{path} is not a regular file")
    if st.st_mode & 0o077:
        raise RuntimeError(f"{path} must be mode 0600 (is {oct(st.st_mode & 0o777)})")
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN_LEN:
        raise RuntimeError(f"{path} holds a token shorter than {MIN_TOKEN_LEN} chars")
    return token


async def _send_status(send, status: int, body: bytes, extra: list | None = None) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8"),
               (b"content-length", str(len(body)).encode())] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerGate:
    """Outermost ASGI layer: no valid bearer, no further processing."""

    def __init__(self, app, token: str):
        if len(token) < MIN_TOKEN_LEN:
            raise ValueError("bearer token too short")
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            # websocket or anything else: never served.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        auth = [v for k, v in scope.get("headers", []) if k == b"authorization"]
        if len(auth) != 1 or not hmac.compare_digest(auth[0], self._expected):
            return await _send_status(send, 401, b"unauthorized\n",
                                      [(b"www-authenticate", b'Bearer realm="ats"')])
        lengths = [v for k, v in scope.get("headers", []) if k == b"content-length"]
        if lengths:
            try:
                if int(lengths[0]) > MAX_BODY_BYTES:
                    return await _send_status(send, 413, b"request too large\n")
            except ValueError:
                return await _send_status(send, 400, b"bad content-length\n")
        return await self.app(scope, _capped_receive(receive), send)


def _capped_receive(receive):
    """Bound a chunked body too: abort once more than MAX_BODY_BYTES arrive."""
    seen = 0

    async def wrapped():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > MAX_BODY_BYTES:
                return {"type": "http.disconnect"}
        return message

    return wrapped


def build_app(token: str):
    """ASGI app: BearerGate -> /mcp Streamable HTTP (stateless, JSON responses)."""
    server = build_server()
    # DNS-rebinding protection is a browser defence for unauthenticated local
    # servers; a browser cannot attach our bearer, so the gate already covers
    # it, and the Funnel host would otherwise have to be pinned here.
    manager = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def mcp_app(scope, receive, send):
        if scope["type"] == "lifespan":
            message = await receive()
            if message["type"] == "lifespan.startup":
                stack = contextlib.AsyncExitStack()
                await stack.enter_async_context(manager.run())
                await send({"type": "lifespan.startup.complete"})
                message = await receive()
                await stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})
            return
        path = scope.get("path", "")
        if path not in ("/mcp", "/mcp/"):
            return await _send_status(send, 404, b"not found\n")
        return await manager.handle_request(scope, receive, send)

    gate = BearerGate(mcp_app, token)
    gate.manager = manager  # tests drive manager.run() themselves
    return gate


def _require_loopback(host: str) -> None:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False
    if not ok:
        raise SystemExit(f"refusing to bind {host!r}: the remote read-only MCP binds "
                         "loopback only; public reach is the Tailscale Funnel")


def run() -> None:
    import uvicorn

    host = os.environ.get("ATS_REMOTE_MCP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("ATS_REMOTE_MCP_PORT", str(DEFAULT_PORT)))
    token_file = Path(os.environ.get("ATS_REMOTE_MCP_TOKEN_FILE", str(DEFAULT_TOKEN_FILE)))
    _require_loopback(host)
    token = load_token(token_file)
    # proxy_headers off: X-Forwarded-* from the Funnel is not trusted for anything.
    uvicorn.run(build_app(token), host=host, port=port, proxy_headers=False,
                server_header=False, log_level="info", access_log=True,
                limit_concurrency=MAX_CONCURRENCY, timeout_keep_alive=5,
                h11_max_incomplete_event_size=MAX_BODY_BYTES)


if __name__ == "__main__":
    run()
