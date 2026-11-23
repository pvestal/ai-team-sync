"""#3787: OAuth for ChatGPT on the remote read-only MCP -- discovery, the code+PKCE
flow, token use on /mcp, refresh rotation, revocation and every refusal."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ai_team_sync.mcp import remote_controls as rc
from ai_team_sync.mcp import remote_oauth as oa
from ai_team_sync.mcp import remote_readonly as rr

BEARER = "b" * 48
CID, SECRET = "tower-chatgpt", "s" * 48
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


class Up(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(200, json={"component": "ats-rest", "commit": "x"})


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(rr, "ATS_URL", "http://127.0.0.1:8400")
    monkeypatch.setattr(rr, "ECHO_URL", "http://127.0.0.1:8309")
    monkeypatch.setattr(rr, "_inner_transport", Up())
    monkeypatch.setattr(rr, "RATE_LIMITER", rc.RateLimiter(per_minute={"call": 1000, "db": 1000}))
    audit = rc.AuditLog(tmp_path / "audit.jsonl")
    monkeypatch.setattr(rr, "AUDIT", audit)
    now = [1_000_000.0]
    store = oa.TokenStore(tmp_path / "oauth-tokens.json", clock=lambda: now[0])
    server = oa.OAuthServer({CID: {"secret_sha256": hashlib.sha256(SECRET.encode()).hexdigest()}},
                            store, audit=audit)
    return {"server": server, "store": store, "now": now, "audit": tmp_path / "audit.jsonl",
            "tokens": tmp_path / "oauth-tokens.json"}


@contextlib.asynccontextmanager
async def serve(env):
    app = rr.build_app({"claude-ai": BEARER}, env["server"])
    async with app.manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8402") as c:
            yield c


def pkce():
    v = secrets.token_urlsafe(48)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def authz(c, challenge, **over):
    q = {"response_type": "code", "client_id": CID, "redirect_uri": REDIRECT,
         "code_challenge": challenge, "code_challenge_method": "S256", "state": "st1",
         "scope": oa.SCOPE, "resource": oa.resource(), **over}
    return c.get("/oauth/authorize", params={k: v for k, v in q.items() if v is not None})


def qs(r):
    return {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}


async def code_for(c):
    v, ch = pkce()
    r = await authz(c, ch)
    assert r.status_code == 302
    return qs(r)["code"], v


async def token(c, **form):
    return await c.post("/oauth/token", data={"client_id": CID, "client_secret": SECRET,
                                              "resource": oa.resource(), **form})


async def mcp(c, tok, method="tools/list", params=None):
    return await c.post("/mcp", headers={"Authorization": f"Bearer {tok}",
                                         "Accept": "application/json, text/event-stream",
                                         "Content-Type": "application/json"},
                        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                            "params": params or {}}))


# ---- discovery -------------------------------------------------------------------------

async def test_discovery_documents(env):
    async with serve(env) as c:
        for path in oa.WELL_KNOWN_PRM:
            prm = (await c.get(path)).json()
            assert prm["resource"] == oa.resource() and prm["authorization_servers"] == [oa.PUBLIC_BASE]
        md = (await c.get(oa.WELL_KNOWN_AS)).json()
    assert md["issuer"] == oa.PUBLIC_BASE
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert md["authorization_response_iss_parameter_supported"] is True
    assert "registration_endpoint" not in md  # no dynamic registration


async def test_unauthenticated_mcp_points_at_the_metadata(env):
    async with serve(env) as c:
        r = await c.post("/mcp", content="{}")
    assert r.status_code == 401
    assert f'resource_metadata="{oa.PUBLIC_BASE}/.well-known/oauth-protected-resource"' in \
        r.headers["www-authenticate"]


async def test_oauth_off_when_no_client_is_configured(env):
    env["server"].clients = {}
    async with serve(env) as c:
        for path in (*oa.WELL_KNOWN_PRM, oa.WELL_KNOWN_AS, "/oauth/authorize"):
            assert (await c.get(path)).status_code == 404
        r = await c.post("/mcp", content="{}")
    assert r.headers["www-authenticate"] == 'Bearer realm="ats"'


# ---- authorize -----------------------------------------------------------------------

@pytest.mark.parametrize("over", [{"client_id": "someone-else"},
                                  {"redirect_uri": "https://evil.example/cb"},
                                  {"redirect_uri": "https://chatgpt.com.evil.example/connector_platform_oauth_redirect"},
                                  {"redirect_uri": REDIRECT + "/../x"}])
async def test_bad_client_or_redirect_is_400_never_a_redirect(env, over):
    async with serve(env) as c:
        r = await authz(c, pkce()[1], **over)
    assert r.status_code == 400 and "location" not in r.headers


@pytest.mark.parametrize("over,err", [({"code_challenge_method": "plain"}, "invalid_request"),
                                      ({"code_challenge": None}, "invalid_request"),
                                      ({"response_type": "token"}, "unsupported_response_type"),
                                      ({"resource": "https://other.example/mcp"}, "invalid_target"),
                                      ({"scope": "tower.write"}, "invalid_scope")])
async def test_authorize_errors_redirect_with_iss_and_state(env, over, err):
    async with serve(env) as c:
        r = await authz(c, pkce()[1], **over)
    assert r.status_code == 302
    q = qs(r)
    assert q["error"] == err and q["iss"] == oa.PUBLIC_BASE and q["state"] == "st1" and "code" not in q


async def test_success_redirect_carries_code_iss_state(env):
    async with serve(env) as c:
        r = await authz(c, pkce()[1])
    q = qs(r)
    assert r.headers["location"].startswith(REDIRECT + "?")
    assert q["iss"] == oa.PUBLIC_BASE and q["state"] == "st1" and len(q["code"]) >= 40


# ---- token + use -----------------------------------------------------------------------

@pytest.mark.parametrize("basic", [False, True])
async def test_full_flow_then_mcp_call_as_the_oauth_client(env, basic):
    async with serve(env) as c:
        code, v = await code_for(c)
        if basic:
            hdr = base64.b64encode(f"{CID}:{SECRET}".encode()).decode()
            r = await c.post("/oauth/token", headers={"Authorization": f"Basic {hdr}"},
                             data={"grant_type": "authorization_code", "code": code,
                                   "code_verifier": v, "redirect_uri": REDIRECT,
                                   "resource": oa.resource()})
        else:
            r = await token(c, grant_type="authorization_code", code=code, code_verifier=v,
                            redirect_uri=REDIRECT)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        t = r.json()
        assert t["token_type"] == "Bearer" and t["scope"] == oa.SCOPE and t["refresh_token"]
        lst = await mcp(c, t["access_token"])
        assert lst.status_code == 200 and len(lst.json()["result"]["tools"]) == 19
        call = await mcp(c, t["access_token"], "tools/call", {"name": "ats_version", "arguments": {}})
        assert call.json()["result"]["isError"] is False
        refused = await mcp(c, t["access_token"], "tools/call",
                            {"name": "start_session", "arguments": {}})
        assert refused.json()["result"]["isError"] is True
    lines = [json.loads(l) for l in env["audit"].read_text().splitlines()]
    assert any(l.get("client") == "oauth:" + CID and l.get("tool") == "ats_version" for l in lines)
    # tokens are stored only as hashes
    raw = env["tokens"].read_text()
    assert t["access_token"] not in raw and t["refresh_token"] not in raw
    assert oct(env["tokens"].stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("mutate", ["verifier", "redirect", "reuse", "secret", "resource", "expired"])
async def test_code_exchange_refusals(env, mutate):
    async with serve(env) as c:
        code, v = await code_for(c)
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": v,
                "redirect_uri": REDIRECT}
        if mutate == "verifier":
            form["code_verifier"] = pkce()[0]
        elif mutate == "redirect":
            form["redirect_uri"] = "https://chatgpt.com/connector/oauth/other"
        elif mutate == "reuse":
            assert (await token(c, **form)).status_code == 200
        elif mutate == "expired":
            env["now"][0] += oa.CODE_TTL_S + 1
        if mutate == "secret":
            r = await c.post("/oauth/token", data={**form, "client_id": CID, "client_secret": "x" * 48})
            assert r.status_code == 401
            return
        if mutate == "resource":
            r = await token(c, **{**form, "resource": "https://other.example/mcp"})
            assert r.status_code == 400 and r.json()["error"] == "invalid_target"
            return
        r = await token(c, **form)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_a_failed_exchange_still_consumes_the_code(env):
    async with serve(env) as c:
        code, v = await code_for(c)
        bad = await token(c, grant_type="authorization_code", code=code,
                          code_verifier=pkce()[0], redirect_uri=REDIRECT)
        good = await token(c, grant_type="authorization_code", code=code,
                           code_verifier=v, redirect_uri=REDIRECT)
    assert bad.status_code == 400 and good.status_code == 400


async def _tokens(c):
    code, v = await code_for(c)
    return (await token(c, grant_type="authorization_code", code=code, code_verifier=v,
                        redirect_uri=REDIRECT)).json()


async def test_refresh_rotates_and_old_tokens_die(env):
    async with serve(env) as c:
        t1 = await _tokens(c)
        r = await token(c, grant_type="refresh_token", refresh_token=t1["refresh_token"])
        assert r.status_code == 200
        t2 = r.json()
        assert (await mcp(c, t1["access_token"])).status_code == 401
        assert (await token(c, grant_type="refresh_token",
                            refresh_token=t1["refresh_token"])).status_code == 400
        assert (await mcp(c, t2["access_token"])).status_code == 200


async def test_access_token_expires(env):
    async with serve(env) as c:
        t = await _tokens(c)
        env["now"][0] += oa.ACCESS_TTL_S + 1
        assert (await mcp(c, t["access_token"])).status_code == 401


async def test_revocation(env):
    async with serve(env) as c:
        t = await _tokens(c)
        r = await c.post("/oauth/revoke", data={"client_id": CID, "client_secret": SECRET,
                                                "token": t["access_token"]})
        assert r.status_code == 200
        assert (await mcp(c, t["access_token"])).status_code == 401
        r = await c.post("/oauth/revoke", data={"client_id": CID, "client_secret": SECRET,
                                                "token": t["refresh_token"]})
        assert (await token(c, grant_type="refresh_token",
                            refresh_token=t["refresh_token"])).status_code == 400


async def test_rotating_or_removing_the_client_secret_ends_every_token(env):
    async with serve(env) as c:
        t = await _tokens(c)
        env["server"].clients[CID]["secret_sha256"] = hashlib.sha256(b"new" * 16).hexdigest()
        assert (await mcp(c, t["access_token"])).status_code == 401
        env["server"].clients = {}
        assert (await mcp(c, t["access_token"])).status_code == 401


async def test_refresh_token_of_another_client_neither_works_nor_revokes(env):
    env["server"].clients["other"] = {"secret_sha256": hashlib.sha256(b"o" * 48).hexdigest()}
    async with serve(env) as c:
        t = await _tokens(c)
        r = await c.post("/oauth/token", data={"client_id": "other", "client_secret": "o" * 48,
                                               "grant_type": "refresh_token",
                                               "refresh_token": t["refresh_token"],
                                               "resource": oa.resource()})
        assert r.status_code == 400
        assert (await token(c, grant_type="refresh_token",
                            refresh_token=t["refresh_token"])).status_code == 200


async def test_token_for_another_audience_is_refused(env):
    async with serve(env) as c:
        t = env["store"].issue(CID, env["server"].clients[CID]["secret_sha256"], oa.SCOPE,
                               "https://other.example/mcp")
        assert (await mcp(c, t["access_token"])).status_code == 401


async def test_an_access_token_is_not_accepted_bare_or_as_a_refresh(env):
    async with serve(env) as c:
        t = await _tokens(c)
        r = await c.post("/mcp", headers={"Authorization": t["access_token"]}, content="{}")
        assert r.status_code == 401
        assert (await token(c, grant_type="refresh_token",
                            refresh_token=t["access_token"])).status_code == 400


async def test_static_bearer_still_works_and_oauth_paths_serve_no_mcp(env):
    async with serve(env) as c:
        assert (await mcp(c, BEARER)).status_code == 200
        r = await c.post("/oauth/authorize", content="{}")
        assert r.status_code == 405
        assert (await c.get("/.well-known/other")).status_code == 401  # not an OAuth path


async def test_token_endpoint_rejects_oversized_body(env):
    async with serve(env) as c:
        r = await c.post("/oauth/token", content="a=" + "x" * (oa.MAX_FORM_BYTES + 10),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


async def test_oauth_audit_has_no_secret_or_code(env):
    async with serve(env) as c:
        t = await _tokens(c)
        await c.post("/oauth/token", data={"client_id": CID, "client_secret": "wrong" * 10,
                                           "grant_type": "authorization_code", "code": "zz"})
    raw = env["audit"].read_text()
    for s in (SECRET, t["access_token"], t["refresh_token"], "wrong" * 10):
        assert s not in raw
    events = [json.loads(l).get("event") for l in raw.splitlines()]
    assert "authorize" in events and "token" in events


def test_clients_file_rules(tmp_path):
    p = tmp_path / "c.json"
    assert oa.load_clients(p) == {}
    p.write_text(json.dumps({"clients": {CID: {"secret_sha256": "a" * 64}}}))
    os.chmod(p, 0o644)
    with pytest.raises(RuntimeError):
        oa.load_clients(p)
    os.chmod(p, 0o600)
    assert oa.load_clients(p) == {CID: {"secret_sha256": "a" * 64}}
    p.write_text(json.dumps({"clients": {"Bad Id": {"secret_sha256": "a" * 64}}}))
    with pytest.raises(RuntimeError):
        oa.load_clients(p)


async def test_r1_a_rotated_secret_sharing_the_old_hash_prefix_still_ends_every_token(env):
    """review r1: tokens were bound to a 16-hex prefix of the secret hash."""
    async with serve(env) as c:
        t = await _tokens(c)
        old = env["server"].clients[CID]["secret_sha256"]
        env["server"].clients[CID]["secret_sha256"] = old[:16] + ("0" if old[16] != "0" else "1") + old[17:]
        assert (await mcp(c, t["access_token"])).status_code == 401
        assert env["store"].rotate_refresh(t["refresh_token"], CID,
                                           env["server"].clients[CID]["secret_sha256"]) is None


# ---- per-client redirect URIs (claude.ai OAuth) --------------------------------------

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


async def test_a_client_with_redirect_uris_may_use_only_those_exact_uris(env):
    env["server"].clients["tower-claude"] = {
        "secret_sha256": hashlib.sha256(b"c" * 48).hexdigest(), "redirect_uris": [CLAUDE_REDIRECT]}
    async with serve(env) as c:
        ok = await authz(c, pkce()[1], client_id="tower-claude", redirect_uri=CLAUDE_REDIRECT)
        assert ok.status_code == 302 and ok.headers["location"].startswith(CLAUDE_REDIRECT + "?")
        for bad in (REDIRECT, CLAUDE_REDIRECT + "x", CLAUDE_REDIRECT + "/../evil",
                    "https://claude.ai.evil.example/api/mcp/auth_callback"):
            r = await authz(c, pkce()[1], client_id="tower-claude", redirect_uri=bad)
            assert r.status_code == 400 and "location" not in r.headers, bad
        # and the ChatGPT client may not use claude's callback
        r = await authz(c, pkce()[1], redirect_uri=CLAUDE_REDIRECT)
        assert r.status_code == 400


async def test_claude_client_full_flow(env):
    env["server"].clients["tower-claude"] = {
        "secret_sha256": hashlib.sha256(b"c" * 48).hexdigest(), "redirect_uris": [CLAUDE_REDIRECT]}
    async with serve(env) as c:
        v, ch = pkce()
        q = qs(await authz(c, ch, client_id="tower-claude", redirect_uri=CLAUDE_REDIRECT))
        r = await c.post("/oauth/token", data={"client_id": "tower-claude", "client_secret": "c" * 48,
                                               "grant_type": "authorization_code", "code": q["code"],
                                               "code_verifier": v, "redirect_uri": CLAUDE_REDIRECT,
                                               "resource": oa.resource()})
        assert r.status_code == 200
        lst = await mcp(c, r.json()["access_token"])
        assert len(lst.json()["result"]["tools"]) == 19


@pytest.mark.parametrize("uris", [[], "https://claude.ai/x", ["http://claude.ai/cb"], ["https://claude.ai/cb?x=1"],
                                  ["https://claude.ai/cb#f"], ["javascript:alert(1)"]])
def test_redirect_uris_must_be_exact_https(tmp_path, uris):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"clients": {"x": {"secret_sha256": "a" * 64, "redirect_uris": uris}}}))
    os.chmod(p, 0o600)
    with pytest.raises(RuntimeError):
        oa.load_clients(p)
