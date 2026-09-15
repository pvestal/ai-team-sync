"""Integration test for the extend_scope MCP tool (the deferred follow-up from
cd71e18 — no harness mocked the MCP httpx client before).

Routes the MCP server's httpx calls into the real in-process ASGI app so the full
path is exercised end-to-end against the DB: GET session -> PATCH merged scope ->
POST a lock per new pattern.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app


@pytest.mark.asyncio
async def test_extend_scope_merges_scope_and_creates_locks(db_engine, monkeypatch):
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    # Point the MCP module's httpx.AsyncClient at the in-process ASGI app. The MCP
    # code uses absolute URLs ({SERVER_URL}/api/...); ASGITransport routes by path
    # regardless of host, so they land on our app.
    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)

    # Seed an active session with one scope pattern, then make it the MCP "active
    # session" the tool operates on.
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.post("/api/sessions", json={
            "developer": "patrick", "agent": "claude-code",
            "scope": ["src/a/**"], "auto_lock": True})
        sid = r.json()["id"]
    monkeypatch.setattr(mcp, "load_session_id", lambda: sid)

    out = await mcp.call_tool("extend_scope", {"patterns": ["src/b/**"], "mode": "advisory"})
    text = out[0].text
    assert "Scope extended" in text
    assert "src/b/**" in text

    # Persisted: scope merged (de-duped, both present) + a lock exists for the new pattern.
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sess = (await c.get(f"/api/sessions/{sid}")).json()
        assert set(sess["scope"]) == {"src/a/**", "src/b/**"}
        patterns = {lock["pattern"] for lock in (await c.get("/api/locks")).json()}
        assert "src/b/**" in patterns


@pytest.mark.asyncio
async def test_extend_scope_reports_refused_locks_and_never_claims_them(db_engine, monkeypatch):
    """#2756: a lock the server refuses is reported as refused and stays out of
    the declared scope; the old path announced 'Scope extended' over it."""
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)

    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        holder = await c.post("/api/sessions", json={
            "developer": "other", "agent": "default", "scope": ["src/b/**"],
            "auto_lock": True, "lock_mode": "exclusive"})
        assert holder.status_code == 201, holder.text
        me = await c.post("/api/sessions", json={
            "developer": "patrick", "agent": "claude-code", "scope": ["src/a/**"], "auto_lock": True})
        sid = me.json()["id"]
    monkeypatch.setattr(mcp, "load_session_id", lambda: sid)

    async def scope_and_locks():
        async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
            scope = set((await c.get(f"/api/sessions/{sid}")).json()["scope"])
            locks = {lock["pattern"] for lock in (await c.get("/api/locks")).json()
                     if lock["session_id"] == sid}
        return scope, locks

    text = (await mcp.call_tool("extend_scope", {"patterns": ["src/b/x.py"], "mode": "exclusive"}))[0].text
    assert "Scope NOT extended" in text
    assert "Scope extended (" not in text
    assert "src/b/x.py: Cannot create lock" in text
    assert "exclusive lock 'src/b/**' held by other" in text
    assert "request_override" in text, "a conflict names the way forward"
    assert await scope_and_locks() == ({"src/a/**"}, {"src/a/**"})

    text = (await mcp.call_tool("extend_scope", {"patterns": ["src/b/y.py", "src/c/**"]}))[0].text
    assert "Scope PARTIALLY extended: +1 advisory lock(s), 1 refused" in text
    assert "src/b/y.py: Cannot create lock" in text
    assert await scope_and_locks() == ({"src/a/**", "src/c/**"}, {"src/a/**", "src/c/**"})

    # A non-409 refusal after a granted lock still reports the granted one.
    text = (await mcp.call_tool("extend_scope", {"patterns": ["src/d/**", "not a glob"]}))[0].text
    assert "Scope PARTIALLY extended: +1 advisory lock(s), 1 refused" in text
    assert "not a glob: HTTP 422" in text
    assert "request_override" not in text, "a refusal with no holder gets no holder advice"
    assert await scope_and_locks() == ({"src/a/**", "src/c/**", "src/d/**"},
                                       {"src/a/**", "src/c/**", "src/d/**"})


@pytest.mark.asyncio
async def test_extend_scope_no_active_session(monkeypatch):
    monkeypatch.setattr(mcp, "load_session_id", lambda: None)
    out = await mcp.call_tool("extend_scope", {"patterns": ["src/b/**"]})
    assert "No active session" in out[0].text
