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
async def test_extend_scope_no_active_session(monkeypatch):
    monkeypatch.setattr(mcp, "load_session_id", lambda: None)
    out = await mcp.call_tool("extend_scope", {"patterns": ["src/b/**"]})
    assert "No active session" in out[0].text
