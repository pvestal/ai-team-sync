"""Integration test for the pre_commit_check MCP tool wiring fix.

The tool used to send {"paths": ...} (endpoint reads `staged_files`) and parse
`blocked`/`warned` (endpoint returns `blocking_locks`/`advisory_locks`), so the
argument was dropped and the response never matched — it ALWAYS said "clear".
This drives the real endpoint through the in-process ASGI app and asserts it now
reports a genuine conflict.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app


def _wire(monkeypatch, db_engine):
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


@pytest.mark.asyncio
async def test_pre_commit_check_reports_exclusive_lock(db_engine, monkeypatch):
    transport = _wire(monkeypatch, db_engine)

    # Another session holds an EXCLUSIVE lock over src/auth/**.
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        await c.post("/api/sessions", json={
            "developer": "alice", "agent": "claude-code",
            "scope": ["src/auth/**"], "auto_lock": True, "lock_mode": "exclusive"})

    out = await mcp.call_tool("pre_commit_check", {"paths": ["src/auth/jwt.py"]})
    text = out[0].text
    assert "BLOCKED" in text                 # was: always "All files clear"
    assert "src/auth/jwt.py" in text
    assert "alice" in text


@pytest.mark.asyncio
async def test_pre_commit_check_clear_when_unlocked(db_engine, monkeypatch):
    _wire(monkeypatch, db_engine)
    out = await mcp.call_tool("pre_commit_check", {"paths": ["src/unrelated/x.py"]})
    assert "All files clear" in out[0].text
