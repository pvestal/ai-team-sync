"""MCP delete_lock lets a live session release its OWN lock (#3581).

The REST endpoint already accepts the holder when it names itself
(?actor_session_id=); the MCP tool never sent it, so every live session got
403 lock_not_yours for its own locks and could not narrow a broad claim short
of completing the session. Runs the real MCP handler against the in-process
app, as tests/test_mcp_extend_scope.py does.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app


@pytest.fixture
def wired(db_engine, monkeypatch):
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
    return transport


async def _session_with_lock(transport, pattern):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.post("/api/sessions", json={
            "developer": "patrick", "agent": "claude-code",
            "scope": [pattern], "auto_lock": True})
        sid = r.json()["id"]
        locks = (await c.get("/api/locks")).json()
        lock_id = next(lk["id"] for lk in locks if lk["session_id"] == sid)
    return sid, lock_id


async def _live_lock_ids(transport):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        return {lk["id"] for lk in (await c.get("/api/locks")).json()}


@pytest.mark.asyncio
async def test_the_holder_releases_its_own_lock_through_mcp(wired, monkeypatch):
    sid, lock_id = await _session_with_lock(wired, "src/a/**")
    monkeypatch.setattr(mcp, "load_session_id", lambda: sid)

    out = await mcp.call_tool("delete_lock", {"lock_id": lock_id})

    assert "Lock deleted" in out[0].text
    assert lock_id not in await _live_lock_ids(wired)


@pytest.mark.asyncio
async def test_another_live_session_still_cannot_drop_it_through_mcp(wired, monkeypatch):
    _owner, lock_id = await _session_with_lock(wired, "src/a/**")
    stranger, _ = await _session_with_lock(wired, "src/b/**")
    monkeypatch.setattr(mcp, "load_session_id", lambda: stranger)

    out = await mcp.call_tool("delete_lock", {"lock_id": lock_id})

    assert "Lock deleted" not in out[0].text
    assert lock_id in await _live_lock_ids(wired)
