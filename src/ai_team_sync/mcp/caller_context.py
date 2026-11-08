"""Request-bound identity and capability state for the ATS MCP server.

One Codex host can multiplex several agent threads through one long-lived MCP
process.  Process environment, pointer files and module globals therefore do
not identify the caller of a particular ``tools/call`` request.  This module
binds mutable MCP state to the request's transport session and, when supplied
by the client, its lifecycle/thread metadata.

The metadata is not an authority grant.  It only chooses an isolated bucket.
Every ownership-sensitive operation still needs the exact session capability
stored in that bucket (or an explicit delegation binding).
"""

from __future__ import annotations

import contextvars
import os
import re
import secrets
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from ai_team_sync import session_pointer as sp
from ai_team_sync.hooks.session_registration import lifecycle_session_key


class CallerContextError(RuntimeError):
    """A request supplied ambiguous or malformed caller metadata."""


@dataclass
class CallerContext:
    """All mutable MCP state belonging to one validated client lifecycle."""

    key: str
    client_id: str
    agent_label: str
    source: str
    host_session_id: str = ""
    default_session_id: str | None = None
    tokens: dict[str, str] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)
    capability_problems: dict[str, str] = field(default_factory=dict)


_BOUND_CALLER: contextvars.ContextVar[CallerContext | None] = contextvars.ContextVar(
    "ats_mcp_bound_caller", default=None
)

# A transport session is allocated by the MCP server, not supplied as a tool
# argument.  A nested map allows a single Codex transport to multiplex threads
# while keeping their state disjoint.  Weak keys retire everything with the
# transport instead of turning caller identity into a process-lifetime cache.
_TRANSPORT_CONTEXTS: weakref.WeakKeyDictionary[Any, dict[str, CallerContext]] = (
    weakref.WeakKeyDictionary()
)
# Some test doubles and third-party ServerSession implementations cannot be
# weak-referenced.  Keep their object alongside id() so id reuse never aliases
# an old caller.  Production's MCP ServerSession is weak-referenceable.
_STRONG_TRANSPORT_CONTEXTS: dict[int, tuple[Any, dict[str, CallerContext]]] = {}
# Metadata-free compatibility is deliberately limited to one transport.  It
# supports older dedicated stdio clients and Claude's /clear lifecycle rotation
# without making the hook-published live id a shared multi-client identity.
_LEGACY_TRANSPORT: Any | None = None
_LEGACY_CODEX_LIFECYCLE = ""
# A delegated MCP process is itself an authority envelope for ONE child caller.
# The delegation capability is stored at process scope for the spawned worker,
# so multiplexing a second lifecycle into that process is ambiguous and must
# fail closed.  This is a one-time binding, not a mutable "current caller".
_DELEGATION_CALLER: tuple[str, str] | None = None

_META_KEYS = (
    "codex/thread_id",
    "codex.thread_id",
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
)
_HOST_SESSION_KEYS = ("sessionId", "session_id", "codex/sessionId",
                      "codex.session_id")
_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}\Z")


def _meta_mapping(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return dict(meta)
    dump = getattr(meta, "model_dump", None)
    if callable(dump):
        value = dump()
        return value if isinstance(value, dict) else {}
    extra = getattr(meta, "model_extra", None)
    return dict(extra) if isinstance(extra, dict) else {}


def client_identity_from_meta(meta: Any) -> str:
    """Return one validated lifecycle id from MCP request metadata.

    Conflicting identities are refused.  Tool arguments are deliberately not
    consulted: ``session_id`` selects a target row but never establishes who
    is asking to act on it.
    """

    data = _meta_mapping(meta)
    nested = data.get("codex")
    if isinstance(nested, dict):
        for key in ("thread_id", "threadId", "conversation_id", "conversationId"):
            if nested.get(key) is not None:
                data[f"codex/{key}"] = nested[key]

    found: list[str] = []
    for key in (*_META_KEYS, "codex/threadId", "codex/conversation_id",
                "codex/conversationId"):
        if key not in data or data[key] is None:
            continue
        value = str(data[key]).strip()
        if not value or not _CLIENT_ID.fullmatch(value):
            raise CallerContextError(
                f"MCP caller identity metadata {key!r} is malformed; refusing "
                "ownership-sensitive operations instead of guessing"
            )
        found.append(value)

    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        raise CallerContextError(
            "MCP caller identity metadata is ambiguous (multiple lifecycle ids); "
            "refusing ownership-sensitive operations"
        )
    return unique[0] if unique else ""


def host_session_from_meta(meta: Any) -> str:
    """Return Codex's process/session id, refusing conflicting aliases."""

    data = _meta_mapping(meta)
    found: list[str] = []
    for key in _HOST_SESSION_KEYS:
        if key not in data or data[key] is None:
            continue
        value = str(data[key]).strip()
        if not value or not _CLIENT_ID.fullmatch(value):
            raise CallerContextError(
                f"MCP caller identity metadata {key!r} is malformed; refusing "
                "ownership-sensitive operations instead of guessing"
            )
        found.append(value)
    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        raise CallerContextError(
            "MCP caller identity metadata is ambiguous (multiple host session ids); "
            "refusing ownership-sensitive operations"
        )
    return unique[0] if unique else ""


def _new_context(base_agent: str, lifecycle_id: str, source: str,
                 host_session_id: str = "") -> CallerContext:
    base = (base_agent or "unknown").strip() or "unknown"
    if lifecycle_id:
        # SessionStart publishes the already-canonical client key (Codex raw
        # thread ids are hashed there). Request metadata carries the raw id.
        client_id = (lifecycle_id if source == "legacy_hook"
                     else lifecycle_session_key(base, lifecycle_id))
        label = sp.agent_label(base, client_id)
        key = f"{base}:{client_id}"
    else:
        # Transport-only fallback is isolated but intentionally not associated
        # with any hook pointer/keyring.  It can open and retain its own sessions
        # for this connection; it cannot adopt another lifecycle's row.
        nonce = secrets.token_hex(16)
        client_id = f"mcp-{nonce}"
        label = base if base == "unknown" else f"{base}:{nonce[:8]}"
        key = f"{base}:transport:{nonce}"
    return CallerContext(key=key, client_id=client_id, agent_label=label,
                         source=source, host_session_id=host_session_id)


def _bucket_for_transport(transport_session: Any) -> dict[str, CallerContext]:
    try:
        bucket = _TRANSPORT_CONTEXTS.get(transport_session)
        if bucket is None:
            bucket = {}
            _TRANSPORT_CONTEXTS[transport_session] = bucket
        return bucket
    except (TypeError, AttributeError):
        ident = id(transport_session)
        entry = _STRONG_TRANSPORT_CONTEXTS.get(ident)
        if entry is None or entry[0] is not transport_session:
            entry = (transport_session, {})
            _STRONG_TRANSPORT_CONTEXTS[ident] = entry
        return entry[1]


def _bind_delegated_process(context: CallerContext) -> None:
    """Bind an explicit delegated-process envelope to exactly one caller.

    A supervisor passes one capability through ``ATS_DELEGATION`` and the
    delegated-capability file.  Those process-scoped credentials cannot safely
    distinguish multiplexed clients, so a second caller is refused rather than
    allowed to adopt the same child authority.
    """

    global _DELEGATION_CALLER

    delegation_id = (os.environ.get("ATS_DELEGATION") or "").strip()
    if not delegation_id:
        return
    binding = (delegation_id, context.key)
    if _DELEGATION_CALLER is None:
        _DELEGATION_CALLER = binding
        return
    if _DELEGATION_CALLER != binding:
        raise CallerContextError(
            "delegated MCP process is already bound to a different caller; "
            "refusing to share its child capability (launch a separate MCP "
            "process for each delegated client)"
        )


def context_for_request(server: Any, base_agent: str) -> CallerContext:
    """Resolve the caller bucket for the current MCP request.

    ``server.request_context`` is a ContextVar maintained by the MCP SDK, so
    concurrent requests cannot overwrite each other's binding.
    """

    global _LEGACY_TRANSPORT, _LEGACY_CODEX_LIFECYCLE

    request = server.request_context  # raises LookupError outside a real request
    lifecycle_id = client_identity_from_meta(request.meta)
    host_session_id = host_session_from_meta(request.meta)
    if (base_agent or "").strip().lower() == "codex" \
            and host_session_id and not lifecycle_id:
        raise CallerContextError(
            "Codex MCP request names sessionId but omits threadId; refusing to "
            "collapse an ambiguous caller onto process-wide state"
        )
    source = "request_meta"
    if not lifecycle_id:
        if _LEGACY_TRANSPORT is None:
            _LEGACY_TRANSPORT = request.session
        elif _LEGACY_TRANSPORT is not request.session:
            raise CallerContextError(
                "multiple metadata-free MCP transports cannot share hook-published "
                "caller identity; upgrade the client to send threadId/sessionId"
            )
        candidate = (sp.claude_session_id() or "").strip()
        if candidate:
            if not _CLIENT_ID.fullmatch(candidate):
                raise CallerContextError(
                    "hook-published MCP caller identity is malformed; refusing "
                    "ownership-sensitive operations"
                )
            lifecycle_id = candidate
            source = "legacy_hook"
            if (base_agent or "").strip().lower() == "codex":
                if _LEGACY_CODEX_LIFECYCLE and \
                        _LEGACY_CODEX_LIFECYCLE != lifecycle_id:
                    raise CallerContextError(
                        "metadata-free Codex caller identity changed inside one MCP "
                        "transport; refusing to guess between clients (send threadId)"
                    )
                _LEGACY_CODEX_LIFECYCLE = lifecycle_id
        else:
            source = "transport"
    bucket = _bucket_for_transport(request.session)
    bucket_key = lifecycle_id or "<transport>"
    existing = bucket.get(bucket_key)
    if existing is not None:
        expected_base = existing.agent_label.split(":", 1)[0]
        if expected_base != (base_agent or "unknown").strip():
            raise CallerContextError(
                f"MCP caller identity changed from {expected_base!r} to "
                f"{base_agent!r} on one client context; refusing the request"
            )
        if existing.host_session_id != host_session_id:
            raise CallerContextError(
                "MCP caller thread changed host session identity; refusing to "
                "reuse its authority bucket"
            )
        _bind_delegated_process(existing)
        return existing
    created = _new_context(base_agent, lifecycle_id, source,
                           host_session_id=host_session_id)
    _bind_delegated_process(created)
    bucket[bucket_key] = created
    return created


def bound_caller() -> CallerContext | None:
    return _BOUND_CALLER.get()


@contextmanager
def bind_caller(context: CallerContext) -> Iterator[CallerContext]:
    """Bind one caller for exactly the current async/request context."""

    token = _BOUND_CALLER.set(context)
    try:
        yield context
    finally:
        _BOUND_CALLER.reset(token)


def isolated_caller(base_agent: str, lifecycle_id: str) -> CallerContext:
    """Deterministic test seam for two callers sharing one module/process."""

    if not lifecycle_id or not _CLIENT_ID.fullmatch(lifecycle_id):
        raise CallerContextError("isolated caller needs a validated lifecycle id")
    return _new_context(base_agent, lifecycle_id, "test")


def reset_context_registry_for_tests() -> None:
    """Tests only: discard request buckets without touching ATS sessions."""

    global _LEGACY_TRANSPORT, _LEGACY_CODEX_LIFECYCLE, _DELEGATION_CALLER
    _TRANSPORT_CONTEXTS.clear()
    _STRONG_TRANSPORT_CONTEXTS.clear()
    _LEGACY_TRANSPORT = None
    _LEGACY_CODEX_LIFECYCLE = ""
    _DELEGATION_CALLER = None
