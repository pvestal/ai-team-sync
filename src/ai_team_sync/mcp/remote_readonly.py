"""Read-only ATS MCP over Streamable HTTP, for remote clients (#3760).

A remote client (claude.ai chat, including voice mode) may READ ATS context --
task briefs, decisions, active work -- and nothing else. This is a
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
import logging
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

# The names a remote caller may invoke. Kept as a literal, not derived from the
# stdio catalog, so adding a tool to ats-mcp can never widen this surface.
# preflight is deliberately ABSENT (operator ruling 2026-11-21, #3760 review r2):
# Echo's memory search, which every preflight runs, refreshes the Redis result
# cache and bumps Qdrant access metadata. It returns when Echo's search takes a
# read-only flag (follow-up ticket). task_brief runs with recall forced off for
# the same reason.
READONLY_TOOL_NAMES = frozenset({
    "task_brief",
    "get_decision_history",
    "team_status",
    "get_session_details",
    "list_all_locks",
    "ats_version",
})

# Every (method, base, path regex) this server may send upstream. GETs are reads
# by HTTP contract on ATS; the one POST is the read-only brief builder (a brief
# never binds a ticket or claims anything, routers/briefs.py). Nothing here
# reaches Echo directly.
_SID = r"[A-Za-z0-9-]{1,64}"
UPSTREAM_ALLOWLIST: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("POST", "ats", re.compile(r"/api/brief")),
    ("GET", "ats", re.compile(r"/api/sessions")),
    ("GET", "ats", re.compile(rf"/api/sessions/{_SID}")),
    ("GET", "ats", re.compile(r"/api/decisions")),
    ("GET", "ats", re.compile(r"/api/locks")),
    ("GET", "ats", re.compile(r"/api/restarts")),
    ("GET", "ats", re.compile(r"/api/version")),
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
    bases = {"ats": _origin(ATS_URL)}
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
        # The size cap counts wire bytes, so the wire must BE the body: ask for
        # no compression and refuse any encoded answer (#3760 review r2 -- a
        # 5 KB gzip body decoded to 5 MB past a raw-byte cap).
        request.headers["Accept-Encoding"] = "identity"
        response = await self._inner.handle_async_request(request)
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            await response.stream.aclose()
            raise UpstreamTooLarge(
                f"{request.method} {request.url.path} answered with content-encoding "
                f"{encoding!r}; only identity is accepted")
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
            "each line with provenance and a citation. Reading it claims nothing. "
            "Semantic memory recall is off on this remote surface."),
        inputSchema={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The work, in a sentence."},
                "task_id": {"type": "integer", "minimum": 1,
                            "description": "Tower task id for exact rulings."},
                "repo_root": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "resolve_task": {"type": "boolean"},
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
                            # Recall goes to Echo memory search, which writes
                            # cache/access metadata; never on this surface.
                            "recall": False,
                            "resolve_task": bool(a.get("resolve_task", True)),
                            # No caller session: a remote reader is nobody's session.
                            "session_id": ""}
    if a.get("task_id"):
        body["task_id"] = int(a["task_id"])
    r = await c.post(f"{ATS_URL}/api/brief", json=body)
    if r.status_code >= 400:
        return _text(f"Task brief refused (HTTP {r.status_code}): {r.text[:500]}")
    return _text(r.json().get("rendered") or json.dumps(r.json(), indent=1)[:20000])


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
    except (UpstreamRefused, UpstreamTooLarge):
        raise
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ats-rest: unreachable ({type(exc).__name__})")
    return _text("\n".join(lines))


_HANDLERS = {
    "task_brief": _task_brief,
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


auth_log = logging.getLogger("ats_remote_mcp.auth")

# Scheme names safe to echo into a log line. Anything else is logged as
# "<other>": a value with no scheme at all would otherwise put the token itself
# in the "scheme" slot.
_KNOWN_SCHEMES = frozenset({"bearer", "basic", "digest", "token", "negotiate"})
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
# RFC 7230 OWS around a field value: SP and HTAB only.
_OWS = b" \t"
# Never valid inside the scheme or the credential.
_CTL_WS = frozenset(b"\t\r\n\x0b\x0c")


def _parse_bearer(value: bytes) -> tuple[str, bytes]:
    """(scheme as logged, credential) from one Authorization value.

    RFC 7235 ``credentials = auth-scheme [ 1*SP token68 ]``: the scheme is
    case-insensitive and separated from the credential by spaces (SP only).
    Optional whitespace around the whole field value is ignored. A tab, CR, LF,
    VT or FF anywhere inside makes the value invalid.
    """
    v = value.strip(_OWS)
    if not v:
        return "<empty>", b""
    scheme_b, sep, rest = v.partition(b" ")
    if any(c in _CTL_WS for c in v):
        scheme = scheme_b.decode("latin-1").lower()
        return (scheme if scheme in _KNOWN_SCHEMES else "<other>"), b""
    if not sep:
        return "<none>", b""
    scheme = scheme_b.decode("latin-1").lower()
    logged = scheme if scheme in _KNOWN_SCHEMES else "<other>"
    return logged, (rest.lstrip(b" ") if scheme == "bearer" else b"")


# Anthropic's published egress range for claude.ai connectors -- the question a
# refusal log has to answer is "was this claude.ai?".
ANTHROPIC_EGRESS = ipaddress.ip_network("160.79.104.0/21")


def _client_addresses(raw: str) -> str:
    """Classify each forwarded client address; never echo it.

    An address is client-chosen bits: a valid IPv6 address can carry 32 hex
    characters of anything, including the secret (review r2 of this change).
    So each comma-separated entry (at most three) is logged only as
    ``anthropic`` (inside ANTHROPIC_EGRESS), ``v4`` / ``v6`` (some other
    address), or ``<invalid>``.
    """
    out = []
    for part in raw.split(",")[:3]:
        part = part.strip()
        try:
            if "%" in part:  # an IPv6 zone id is free text
                raise ValueError(part)
            addr = ipaddress.ip_address(part)
        except ValueError:
            out.append("<invalid>")
            continue
        if addr.version == 6 and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if addr in ANTHROPIC_EGRESS:
            out.append("anthropic")
        else:
            out.append(f"v{addr.version}")
    return ",".join(out) or "-"


class BearerGate:
    """Outermost ASGI layer: no valid bearer, no further processing.

    Every refusal is logged with what arrived -- how many Authorization
    headers, the scheme, the credential length against the expected length, and
    the forwarded client address -- and never the credential itself.
    """

    def __init__(self, app, token: str):
        if len(token) < MIN_TOKEN_LEN:
            raise ValueError("bearer token too short")
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            # websocket or anything else: never served.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = scope.get("headers", [])
        auth = [v for k, v in headers if k == b"authorization"]
        scheme, credential = _parse_bearer(auth[0]) if len(auth) == 1 else ("-", b"")
        ok = (len(auth) == 1 and scheme == "bearer"
              and hmac.compare_digest(credential, self._token))
        if not ok:
            self._log_refusal(scope, headers, auth, scheme, credential)
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

    def _log_refusal(self, scope, headers, auth, scheme, credential) -> None:
        xff = ""
        funnel = False
        for k, v in headers:
            if k == b"x-forwarded-for" and not xff:
                xff = v.decode("latin-1")
            elif k == b"tailscale-funnel-request":
                funnel = True
        # Every logged field is either a number, a member of a fixed set, or a
        # parsed IP address: nothing a client sends is echoed as text.
        method = str(scope.get("method", ""))
        path = scope.get("path", "")
        auth_log.warning(
            "401 %s %s auth_headers=%d scheme=%s credential_len=%d expected_len=%d "
            "len_match=%s xff=%s funnel=%s",
            method if method in _KNOWN_METHODS else "<other>",
            path if path in ("/mcp", "/mcp/") else "<other>",
            len(auth), scheme, len(credential), len(self._token),
            len(credential) == len(self._token),
            _client_addresses(xff) if xff else "-", "yes" if funnel else "no")


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
    # The refusal log must reach the journal whatever else configured logging:
    # its own stderr handler, not dependent on a root handler.
    if not auth_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        auth_log.addHandler(handler)
        auth_log.setLevel(logging.INFO)
        auth_log.propagate = False
    # proxy_headers off: X-Forwarded-* from the Funnel is not trusted for anything.
    uvicorn.run(build_app(token), host=host, port=port, proxy_headers=False,
                server_header=False, log_level="info", access_log=True,
                limit_concurrency=MAX_CONCURRENCY, timeout_keep_alive=5,
                h11_max_incomplete_event_size=MAX_BODY_BYTES)


if __name__ == "__main__":
    run()
