"""Minimal OAuth 2.1 authorization server for the remote read-only MCP (#3787).

ChatGPT cannot send a static bearer header; it connects to a remote MCP server
only through OAuth. This module is the smallest server ChatGPT needs, inside the
existing ats-mcp-remote process (no second server):

* discovery: RFC 9728 protected-resource metadata and RFC 8414 authorization-
  server metadata;
* /oauth/authorize: authorization code + PKCE S256 only, RFC 9207 ``iss`` in
  every redirect, exact ``resource`` binding;
* /oauth/token: authorization_code and refresh_token grants, confidential
  clients only (client_secret_basic / client_secret_post), refresh rotation;
* /oauth/revoke: RFC 7009.

Who may connect: only clients listed in the operator's 0600 clients file, each
with its own secret (stored as a SHA-256, never in the clear) and redirect URIs
limited to ChatGPT's. The secret is the credential, exactly as the #3760 bearer
secret is for claude.ai: without it no code can be exchanged for a token. A
token grants scope ``tower.read`` for the one resource ``<base>/mcp`` and is
validated by BearerGate like any other client credential, so it gets the same
read-only tool surface, rate limits and audit -- never more. Removing a client
from the file, or changing its secret, invalidates all of its tokens at once.

Tokens are opaque random strings; only their SHA-256 is stored (0600 state
file), so a stolen state file grants nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

PUBLIC_BASE = os.environ.get("ATS_REMOTE_PUBLIC_BASE", "https://tower.tail5aaf78.ts.net").rstrip("/")
SCOPE = "tower.read"
CODE_TTL_S = 120
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 86400
MAX_FORM_BYTES = 8 * 1024

# ChatGPT's two redirect forms (developers.openai.com/apps-sdk/build/auth, read
# 2026-11-23): the fixed one when the server advertises RFC 9207 iss support,
# otherwise a per-connector callback. Nothing else is ever a redirect target.
CHATGPT_REDIRECT = re.compile(
    r"https://chatgpt\.com/(?:connector_platform_oauth_redirect|connector/oauth/[A-Za-z0-9_-]{1,128})")
_PKCE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_CLIENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

WELL_KNOWN_PRM = ("/.well-known/oauth-protected-resource",
                  "/.well-known/oauth-protected-resource/mcp")
WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
PATHS = frozenset({*WELL_KNOWN_PRM, WELL_KNOWN_AS, "/oauth/authorize", "/oauth/token",
                   "/oauth/revoke"})


def resource() -> str:
    return PUBLIC_BASE + "/mcp"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def protected_resource_metadata() -> dict:
    return {"resource": resource(), "authorization_servers": [PUBLIC_BASE],
            "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"]}


def authorization_server_metadata() -> dict:
    return {
        "issuer": PUBLIC_BASE,
        "authorization_endpoint": PUBLIC_BASE + "/oauth/authorize",
        "token_endpoint": PUBLIC_BASE + "/oauth/token",
        "revocation_endpoint": PUBLIC_BASE + "/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": [SCOPE],
        "authorization_response_iss_parameter_supported": True,
    }


# ---- clients (operator-managed file) ----------------------------------------------

def load_clients(path: Path) -> dict[str, dict]:
    """{client_id: {"secret_sha256": hex}}; a missing file means OAuth is off."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise RuntimeError(f"{path} must be a regular 0600 file")
    data = json.loads(path.read_text()).get("clients", {})
    out = {}
    for cid, rec in data.items():
        sha = str(rec.get("secret_sha256", ""))
        if not _CLIENT_ID.fullmatch(cid) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise RuntimeError(f"{path}: bad client entry {cid!r}")
        out[cid] = {"secret_sha256": sha}
    return out


# ---- token store ----------------------------------------------------------------------

class TokenStore:
    """Hashes of issued tokens, persisted (0600) so a restart does not log ChatGPT out."""

    def __init__(self, path: Path | None, clock=time.time):
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        self._codes: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}
        if path and path.exists():
            try:
                self._tokens = json.loads(path.read_text()).get("tokens", {})
            except (OSError, ValueError):
                self._tokens = {}

    def _save(self) -> None:
        if not self.path:
            return
        now = self.clock()
        self._tokens = {h: t for h, t in self._tokens.items() if t["exp"] > now}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tokens": self._tokens}, f)
        os.replace(tmp, self.path)

    def new_code(self, rec: dict) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._codes = {k: v for k, v in self._codes.items() if v["exp"] > now}
            self._codes[_sha(code)] = {**rec, "exp": now + CODE_TTL_S}
        return code

    def take_code(self, code: str) -> dict | None:
        with self._lock:
            rec = self._codes.pop(_sha(code), None)  # single use, even when it fails
        if rec is None or rec["exp"] <= self.clock():
            return None
        return rec

    def issue(self, client_id: str, secret_sha: str, scope: str, aud: str) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.clock()
        base = {"client": client_id, "csec": secret_sha, "scope": scope, "aud": aud}
        with self._lock:
            self._tokens[_sha(access)] = {**base, "kind": "access", "exp": now + ACCESS_TTL_S}
            self._tokens[_sha(refresh)] = {**base, "kind": "refresh", "exp": now + REFRESH_TTL_S,
                                           "access": _sha(access)}
            self._save()
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                "refresh_token": refresh, "scope": scope}

    def lookup(self, token: str, kind: str) -> dict | None:
        rec = self._tokens.get(_sha(token))
        if not rec or rec["kind"] != kind or rec["exp"] <= self.clock():
            return None
        return rec

    def revoke(self, token: str, client_id: str) -> None:
        h = _sha(token)
        with self._lock:
            rec = self._tokens.get(h)
            if rec and rec["client"] == client_id:
                self._tokens.pop(h, None)
                if rec["kind"] == "refresh" and rec.get("access"):
                    self._tokens.pop(rec["access"], None)
                self._save()

    def rotate_refresh(self, token: str, client_id: str, csec: str) -> dict | None:
        """Consume a refresh token -- only when it is this client's, live, and
        issued under the client's current secret (so presenting someone else's
        token can never revoke it)."""
        h = _sha(token)
        with self._lock:
            rec = self._tokens.get(h)
            if (not rec or rec["kind"] != "refresh" or rec["exp"] <= self.clock()
                    or rec["client"] != client_id
                    or not hmac.compare_digest(str(rec.get("csec", "")), csec)):
                return None
            self._tokens.pop(h, None)
            if rec.get("access"):
                self._tokens.pop(rec["access"], None)
            self._save()
        return rec


# ---- the server ---------------------------------------------------------------------

class OAuthServer:
    def __init__(self, clients: dict[str, dict], store: TokenStore, audit=None,
                 limiter=None):
        self.clients, self.store, self.audit, self.limiter = clients, store, audit, limiter

    @property
    def enabled(self) -> bool:
        return bool(self.clients)

    # -- used by BearerGate --
    def client_for_access_token(self, token: bytes) -> str | None:
        if not self.enabled or not token:
            return None
        try:
            rec = self.store.lookup(token.decode("ascii"), "access")
        except UnicodeDecodeError:
            return None
        if not rec or rec["aud"] != resource() or rec["scope"] != SCOPE:
            return None
        client = self.clients.get(rec["client"])
        # a removed client, or a changed secret, ends every token it was issued
        if not client or not hmac.compare_digest(client["secret_sha256"], str(rec.get("csec", ""))):
            return None
        return "oauth:" + rec["client"]

    def www_authenticate(self) -> bytes:
        return (f'Bearer resource_metadata="{PUBLIC_BASE}{WELL_KNOWN_PRM[0]}", '
                f'scope="{SCOPE}"').encode()

    # -- ASGI --
    async def __call__(self, scope, receive, send):
        path, method = scope.get("path", ""), scope.get("method", "")
        if not self.enabled:
            return await _respond(send, 404, b"not found\n", "text/plain")
        if path in WELL_KNOWN_PRM and method == "GET":
            return await _json(send, 200, protected_resource_metadata())
        if path == WELL_KNOWN_AS and method == "GET":
            return await _json(send, 200, authorization_server_metadata())
        if self.limiter is not None and not self.limiter.allow("oauth-endpoints", "call"):
            return await _json(send, 429, {"error": "slow_down"})
        if path == "/oauth/authorize" and method == "GET":
            return await self._authorize(scope, send)
        if path in ("/oauth/token", "/oauth/revoke") and method == "POST":
            body = await _read_form(receive)
            if body is None:
                return await _json(send, 413, {"error": "invalid_request"})
            headers = dict(scope.get("headers", []))
            if path == "/oauth/token":
                return await self._token(body, headers, send)
            return await self._revoke(body, headers, send)
        return await _respond(send, 405, b"method not allowed\n", "text/plain")

    def _record(self, event: str, outcome: str, client: str | None = None) -> None:
        if self.audit is not None:
            self.audit.record({"tool": "<oauth>", "event": event, "outcome": outcome,
                               "client": ("oauth:" + client) if client in self.clients else "?"})

    async def _authorize(self, scope, send):
        q = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode("latin-1")).items()
             if len(v) == 1}
        client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
        # Never redirect to an unvalidated client or URI (RFC 6749 4.1.2.1).
        if client_id not in self.clients or not CHATGPT_REDIRECT.fullmatch(redirect_uri):
            self._record("authorize", "refused_client_or_redirect")
            return await _respond(send, 400, b"invalid client or redirect_uri\n", "text/plain")
        state = q.get("state")

        def back(params: dict) -> Any:
            params = {**params, "iss": PUBLIC_BASE, **({"state": state} if state else {})}
            sep = "&" if "?" in redirect_uri else "?"
            return _redirect(send, redirect_uri + sep + urlencode(params))

        if q.get("response_type") != "code":
            self._record("authorize", "unsupported_response_type", client_id)
            return await back({"error": "unsupported_response_type"})
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not _PKCE.fullmatch(challenge):
            self._record("authorize", "pkce_required", client_id)
            return await back({"error": "invalid_request",
                               "error_description": "PKCE S256 required"})
        if q.get("resource", resource()) != resource():
            self._record("authorize", "invalid_target", client_id)
            return await back({"error": "invalid_target"})
        requested = set((q.get("scope") or SCOPE).split())
        if not requested <= {SCOPE}:
            self._record("authorize", "invalid_scope", client_id)
            return await back({"error": "invalid_scope"})
        code = self.store.new_code({"client": client_id, "redirect_uri": redirect_uri,
                                    "challenge": challenge, "resource": resource()})
        self._record("authorize", "code_issued", client_id)
        return await back({"code": code})

    def _client_auth(self, form: dict, headers: dict) -> str | None:
        cid, secret = form.get("client_id"), form.get("client_secret")
        auth = headers.get(b"authorization", b"")
        if auth[:6].lower() == b"basic ":
            try:
                raw = base64.b64decode(auth[6:].strip(), validate=True).decode()
                from urllib.parse import unquote
                cid, _, secret = raw.partition(":")
                cid, secret = unquote(cid), unquote(secret)
            except Exception:  # noqa: BLE001 -- any malformed header is a failed auth
                return None
        if not cid or not secret or cid not in self.clients:
            # still spend a comparison so timing does not reveal valid client ids
            hmac.compare_digest(_sha(secret or ""), "0" * 64)
            return None
        ok = hmac.compare_digest(_sha(secret), self.clients[cid]["secret_sha256"])
        return cid if ok else None

    async def _token(self, form: dict, headers: dict, send):
        cid = self._client_auth(form, headers)
        if cid is None:
            self._record("token", "invalid_client")
            return await _json(send, 401, {"error": "invalid_client"},
                               [(b"www-authenticate", b'Basic realm="tower-oauth"')])
        csec = self.clients[cid]["secret_sha256"]
        grant = form.get("grant_type")
        if form.get("resource", resource()) != resource():
            self._record("token", "invalid_target", cid)
            return await _json(send, 400, {"error": "invalid_target"})
        if grant == "authorization_code":
            rec = self.store.take_code(form.get("code", ""))
            verifier = form.get("code_verifier", "")
            if (not rec or rec["client"] != cid
                    or rec["redirect_uri"] != form.get("redirect_uri", "")
                    or not _PKCE.fullmatch(verifier)
                    or not hmac.compare_digest(
                        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                        .rstrip(b"=").decode(), rec["challenge"])):
                self._record("token", "invalid_grant", cid)
                return await _json(send, 400, {"error": "invalid_grant"})
            self._record("token", "issued", cid)
            return await _json(send, 200, self.store.issue(cid, csec, SCOPE, rec["resource"]))
        if grant == "refresh_token":
            rec = self.store.rotate_refresh(form.get("refresh_token", ""), cid, csec)
            if not rec:
                self._record("token", "invalid_grant", cid)
                return await _json(send, 400, {"error": "invalid_grant"})
            self._record("token", "refreshed", cid)
            return await _json(send, 200, self.store.issue(cid, csec, SCOPE, rec["aud"]))
        self._record("token", "unsupported_grant_type", cid)
        return await _json(send, 400, {"error": "unsupported_grant_type"})

    async def _revoke(self, form: dict, headers: dict, send):
        cid = self._client_auth(form, headers)
        if cid is None:
            self._record("revoke", "invalid_client")
            return await _json(send, 401, {"error": "invalid_client"})
        self.store.revoke(form.get("token", ""), cid)
        self._record("revoke", "ok", cid)
        return await _json(send, 200, {})


# ---- ASGI helpers -------------------------------------------------------------------

async def _read_form(receive) -> dict | None:
    body, more = b"", True
    while more:
        msg = await receive()
        if msg["type"] != "http.request":
            return None
        body += msg.get("body", b"")
        if len(body) > MAX_FORM_BYTES:
            return None
        more = msg.get("more_body", False)
    form = parse_qs(body.decode("latin-1"), keep_blank_values=True)
    return {k: v[0] for k, v in form.items() if len(v) == 1}


async def _respond(send, status: int, body: bytes, ctype: str, extra: list | None = None):
    headers = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), (b"pragma", b"no-cache")] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send, status: int, obj: dict, extra: list | None = None):
    await _respond(send, status, json.dumps(obj).encode(), "application/json", extra)


async def _redirect(send, location: str):
    await send({"type": "http.response.start", "status": 302,
                "headers": [(b"location", location.encode()), (b"content-length", b"0"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": b""})
