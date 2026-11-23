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
import time
from pathlib import Path
from typing import Any

import httpx
import jsonschema
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, Tool

from ai_team_sync.mcp import remote_tower as tower
from ai_team_sync.mcp.remote_controls import AuditLog, RateLimiter, describe_args, load_clients

ATS_URL = os.environ.get("ATS_SERVER_URL", "http://127.0.0.1:8400").rstrip("/")
# Echo is read for exactly two SELECT-only routes (#3787): the task envelope and
# /health. Anime Studio is never an upstream: in single_user auth mode every
# request to it is the operator, and several of its GETs write.
ECHO_URL = os.environ.get("ATS_REMOTE_ECHO_URL", "http://127.0.0.1:8309").rstrip("/")

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
ATS_TOOL_NAMES = frozenset({
    "task_brief",
    "get_decision_history",
    "team_status",
    "get_session_details",
    "list_all_locks",
    "ats_version",
})
# #3787 Phase 1: Echo / Anime Studio / service-state reads (remote_tower.py).
READONLY_TOOL_NAMES = ATS_TOOL_NAMES | tower.TOWER_TOOL_NAMES

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
    # #3787: Echo's SELECT-only task envelope (tower_task_api.py) and /health.
    ("GET", "echo", re.compile(r"/api/tower-tasks/[1-9][0-9]{0,9}")),
    ("GET", "echo", re.compile(r"/health")),
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


# The only origins this process may ever reach, pinned in code (review r4: the
# configurable ATS_SERVER_URL / ATS_REMOTE_ECHO_URL could otherwise name Anime
# Studio :8401 as "ats" or "echo"). Environment may only restate them.
PINNED_ORIGINS = {"ats": ("http", "127.0.0.1", 8400), "echo": ("http", "127.0.0.1", 8309)}


class OriginNotPinned(RuntimeError):
    """ATS_URL or ECHO_URL names an origin other than its pinned one."""


def require_pinned_origins() -> None:
    for base, url in (("ats", ATS_URL), ("echo", ECHO_URL)):
        if _origin(url) != PINNED_ORIGINS[base]:
            raise OriginNotPinned(f"{base} upstream must be "
                                  "%s://%s:%d" % PINNED_ORIGINS[base])


def upstream_allowed(method: str, url: httpx.URL) -> bool:
    origin = (url.scheme, url.host, url.port)
    bases = {"ats": _origin(ATS_URL), "echo": _origin(ECHO_URL)}
    # Both the configured base AND the pinned origin must agree, so a mistaken
    # environment value refuses everything instead of widening anything.
    if any(bases[b] != PINNED_ORIGINS[b] for b in bases):
        return False
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


_ATS_TOOLS: list[Tool] = [
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

# Every tool is marked read-only for clients that honour annotations (ChatGPT
# treats a tool without readOnlyHint as a write that needs confirmation).
for _t in _ATS_TOOLS:
    _t.annotations = tower.READ_ONLY
READONLY_TOOLS: list[Tool] = _ATS_TOOLS + tower.TOWER_TOOLS
assert {t.name for t in READONLY_TOOLS} == READONLY_TOOL_NAMES
# The argument names each tool declares: the only keys the audit log may name.
TOOL_ARG_KEYS = {t.name: frozenset(t.inputSchema.get("properties", {})) for t in READONLY_TOOLS}
TOOL_SCHEMAS = {t.name: t.inputSchema for t in READONLY_TOOLS}


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
assert set(_HANDLERS) == ATS_TOOL_NAMES


async def _dispatch(c: httpx.AsyncClient, name: str, a: dict[str, Any]) -> list[TextContent]:
    if name in _HANDLERS:
        return await _HANDLERS[name](c, a)
    if name == "service_state":
        return await tower.h_service_state(c, a, ECHO_URL, ATS_URL)
    return await tower.TOWER_HANDLERS[name](c, a, ECHO_URL)


# Process-wide controls; run() points the audit log at the state directory.
RATE_LIMITER = RateLimiter()
AUDIT = AuditLog(None)



def _caller(server: Server) -> str:
    """The client id BearerGate attached to this HTTP request ("?" if none)."""
    try:
        request = server.request_context.request
        return str(request.scope.get("ats_remote_client") or "?")
    except (LookupError, AttributeError):
        return "?"


def build_server() -> Server:
    server: Server = Server("ats-remote-readonly")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return list(READONLY_TOOLS)

    # validate_input=False (review r3): the SDK validated arguments BEFORE this
    # callback, so a malformed call skipped the rate limit and the audit. The
    # same jsonschema validation runs below, after both.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        # Raising makes the MCP layer answer isError=true, so a refusal can never
        # be read as a successful result.
        client = _caller(server)
        a = arguments if isinstance(arguments, dict) else {}
        started = time.monotonic()
        entry: dict[str, Any] = {"client": client}
        audit = True
        try:
            # Every call, listed or not, spends the client's budget first
            # (review r2: unlisted names were refused before the limiter).
            if not RATE_LIMITER.allow(client, "call") or (
                    name in tower.DB_TOOLS and not RATE_LIMITER.allow(client, "db")):
                entry.update(tool=name if name in READONLY_TOOL_NAMES else "<unlisted>",
                             outcome="rate_limited")
                audit, suppressed = RATE_LIMITER.should_record_refusal(client)
                if suppressed:
                    entry["suppressed_refusals"] = suppressed
                raise PermissionError("Refused: rate limit for this client reached; "
                                      "retry in a minute.")
            if name not in READONLY_TOOL_NAMES:
                # The name is client text: never logged as such.
                entry.update(tool="<unlisted>", name_len=len(str(name)), outcome="refused")
                raise PermissionError(f"Refused: {name!r} is not available on the "
                                      "read-only remote surface.")
            entry.update(tool=name, args=describe_args(
                a, tower.AUDIT_ENUMS.get(name), TOOL_ARG_KEYS[name]))
            try:
                jsonschema.validate(instance=a, schema=TOOL_SCHEMAS[name])
            except jsonschema.ValidationError:
                entry["outcome"] = "bad_argument"
                # The validator's message can quote client text; say only what failed.
                raise ValueError("Input validation error: the arguments do not match "
                                 f"{name}'s input schema") from None
            async with _client() as c:
                try:
                    result = await _dispatch(c, name, a)
                except UpstreamRefused as exc:
                    entry["outcome"] = "refused_upstream"
                    raise PermissionError(f"Refused: {exc}") from None
                except tower.BadArgument as exc:
                    entry["outcome"] = "bad_argument"
                    raise ValueError(str(exc)) from None
                except tower.NotConfigured as exc:
                    entry["outcome"] = "not_configured"
                    raise RuntimeError(f"Unavailable on this server: {exc}") from None
                except tower.RoleRefused as exc:
                    entry["outcome"] = "role_refused"
                    raise PermissionError(f"Refused: the read-only database role "
                                          f"failed its self-check ({exc})") from None
                except httpx.HTTPError as exc:
                    entry["outcome"] = "upstream_error"
                    raise RuntimeError(f"Upstream read failed: {type(exc).__name__}") from None
                except Exception as exc:  # database / probe errors: name only
                    entry["outcome"] = "error"
                    raise RuntimeError(f"Read failed: {type(exc).__name__}") from None
            # One output bound for every tool (review r2: Tower handlers built
            # TextContent directly and bypassed the 60,000-character cap).
            result = [TextContent(type="text", text=_text(t.text)[0].text)
                      if isinstance(t, TextContent) else t for t in result]
            entry["outcome"] = "ok"
            entry["chars"] = sum(len(t.text) for t in result if isinstance(t, TextContent))
            return result
        finally:
            entry["ms"] = int((time.monotonic() - started) * 1000)
            if audit:
                AUDIT.record(entry)

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


# Anthropic's published OUTBOUND ranges ("stable IP addresses that Anthropic
# uses for outbound requests, for example MCP tool calls", platform.claude.com
# /docs/en/api/ip-addresses, read 2026-11-21) -- the question a refusal log has
# to answer is "was this claude.ai?".
ANTHROPIC_EGRESS = (ipaddress.ip_network("160.79.104.0/21"),
                    ipaddress.ip_network("153.61.208.0/20"))


def _bare_value(value: bytes) -> bytes:
    """The whole Authorization value as a bare credential, or b"" if it cannot be one.

    Surrounding OWS is ignored; any SP or control whitespace inside means it is
    not a single bare token.
    """
    v = value.strip(_OWS)
    if not v or b" " in v or any(c in _CTL_WS for c in v):
        return b""
    return v


def _client_addresses(raw: str) -> str:
    """Classify each forwarded client address; never echo it.

    An address is client-chosen bits: a valid IPv6 address can carry 32 hex
    characters of anything, including the secret (review r2 of this change).
    So each comma-separated entry (at most three) is logged only as
    ``anthropic`` (inside one of the ANTHROPIC_EGRESS ranges), ``v4`` / ``v6`` (some other
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
        if any(addr in net for net in ANTHROPIC_EGRESS):
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

    def __init__(self, app, clients: "str | dict[str, str]"):
        # A bare string is the #3760 single secret; it belongs to client "claude-ai".
        if isinstance(clients, str):
            clients = {"claude-ai": clients}
        if not clients or any(len(t) < MIN_TOKEN_LEN for t in clients.values()):
            raise ValueError("bearer token too short")
        if len(set(clients.values())) != len(clients):
            raise ValueError("two clients share one secret")
        self.app = app
        self._clients = [(cid, t.encode()) for cid, t in clients.items()]
        # The refusal log reports the expected length of the first (primary) secret.
        self._token = self._clients[0][1]

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
        # Two accepted forms, both compared in constant time and both computed
        # every request: "Bearer <secret>" (RFC 7235), and the bare secret as
        # the whole value (claude.ai's connector sends the header value without
        # a scheme -- operator ruling 2026-11-21, #3760).
        bare = _bare_value(auth[0]) if len(auth) == 1 else b""
        # Third form: "Bearer" immediately followed by the secret, no space --
        # what claude.ai's connector actually sent (value_len = 6 + 64, operator
        # ruling 2026-11-21). The prefix is matched case-insensitively; the
        # secret still has to match exactly.
        joined = bare[6:] if bare[:6].lower() == b"bearer" else b""
        # Every client's secret is compared in all three forms on every request,
        # so timing does not depend on which client (if any) matched.
        matched = None
        for cid, secret in self._clients:
            bearer_ok = hmac.compare_digest(credential, secret) and scheme == "bearer"
            bare_ok = hmac.compare_digest(bare, secret)
            joined_ok = hmac.compare_digest(joined, secret)
            if (bearer_ok or bare_ok or joined_ok) and matched is None:
                matched = cid
        ok = len(auth) == 1 and matched is not None
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
        scope["ats_remote_client"] = matched
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
            "401 %s %s auth_headers=%d value_len=%d scheme=%s credential_len=%d "
            "expected_len=%d len_match=%s xff=%s funnel=%s",
            method if method in _KNOWN_METHODS else "<other>",
            path if path in ("/mcp", "/mcp/") else "<other>",
            # value_len: the raw header value's total length (one header only).
            len(auth), len(auth[0]) if len(auth) == 1 else 0,
            scheme, len(credential), len(self._token),
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


def build_app(token: "str | dict[str, str]"):
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


class _Withheld(logging.Formatter):
    """For library loggers (mcp, uvicorn errors): level and logger name only.

    Their messages can carry client-chosen text -- the MCP SDK logs an unlisted
    tool's name verbatim ("Tool '<name>' not listed") -- which this surface
    never echoes (#3787; the same rule as the #3760 refusal log).
    """

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname}:{record.name}:<message withheld>"


def configure_logging(stream=None) -> list[tuple[logging.Logger, logging.Handler]]:
    """Journal handlers for this process. Returns what it attached (tests detach it)."""
    from ai_team_sync.mcp.remote_controls import audit_log
    attached = []
    for lg in (auth_log, audit_log):
        if not lg.handlers:
            h = logging.StreamHandler(stream)
            h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            lg.propagate = False
            attached.append((lg, h))
    for name in ("mcp", "uvicorn.error"):
        lg = logging.getLogger(name)
        h = logging.StreamHandler(stream)
        h.setFormatter(_Withheld())
        lg.addHandler(h)
        lg.setLevel(logging.WARNING)
        lg.propagate = False
        attached.append((lg, h))
    return attached


def run() -> None:
    import uvicorn

    host = os.environ.get("ATS_REMOTE_MCP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("ATS_REMOTE_MCP_PORT", str(DEFAULT_PORT)))
    token_file = Path(os.environ.get("ATS_REMOTE_MCP_TOKEN_FILE", str(DEFAULT_TOKEN_FILE)))
    _require_loopback(host)
    try:
        require_pinned_origins()
    except OriginNotPinned as exc:
        raise SystemExit(f"refusing to start: {exc}") from None
    load_token(token_file)  # same refusals as before (#3760), then per-client (#3787)
    clients_dir = os.environ.get("ATS_REMOTE_MCP_CLIENTS_DIR")
    token = load_clients(token_file,
                         os.environ.get("ATS_REMOTE_MCP_PRIMARY_CLIENT", "claude-ai"),
                         Path(clients_dir) if clients_dir else None)
    state_dir = os.environ.get("ATS_STATE_DIR")
    global AUDIT
    AUDIT = AuditLog(Path(state_dir) / "audit.jsonl" if state_dir else None)
    configure_logging()
    # proxy_headers off: X-Forwarded-* from the Funnel is not trusted for anything.
    # access_log off (#3787): it echoed client-chosen paths and query strings into
    # the journal; tool calls are in the audit log, refusals in the auth log.
    # log_config=None keeps uvicorn from replacing the handlers set above.
    uvicorn.run(build_app(token), host=host, port=port, proxy_headers=False,
                server_header=False, log_level="info", access_log=False, log_config=None,
                limit_concurrency=MAX_CONCURRENCY, timeout_keep_alive=5,
                h11_max_incomplete_event_size=MAX_BODY_BYTES)


if __name__ == "__main__":
    run()
