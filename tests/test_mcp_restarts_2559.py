"""The MCP half of #2559: recording a restart, and SEEING one in team_status.

The record is only half the fix. team_status renders `Decisions: N` -- a count,
never the content -- so a restart logged as a generic decision was invisible to
every other session. If restarts were merely stored and never surfaced, this would
be one more write-only log. These tests pin the surfacing.

Routes the MCP server's httpx client into the in-process ASGI app, the harness
established by test_mcp_extend_scope.
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
    """Point the MCP module at an in-process app and hand back a direct client."""
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
    monkeypatch.setattr(mcp, "load_session_id", lambda: None)
    monkeypatch.setattr(mcp, "get_git_user", lambda: "patrick")
    return transport


def _direct(transport):
    return AsyncClient(transport=transport, base_url="http://localhost:8400")


@pytest.mark.asyncio
async def test_the_tools_are_registered(wired):
    names = {t.name for t in await mcp.list_tools()}
    assert {"record_restart", "recent_restarts"} <= names


@pytest.mark.asyncio
async def test_record_restart_persists_without_a_session(wired):
    """The operator's out-of-band bounce has no ATS session and must still land."""
    out = await mcp.call_tool("record_restart", {
        "unit": "comfyui.service",
        "reason": "idle recycle to reclaim VRAM, operator authorized",
        "old_pid": 111, "new_pid": 222,
        "before": {"ram_avail_gb": 22.7},
    })
    text = out[0].text
    assert "comfyui" in text and "111 -> 222" in text

    async with _direct(wired) as c:
        rows = (await c.get("/api/restarts")).json()
    assert len(rows) == 1
    assert rows[0]["unit"] == "comfyui", "normalized on the way in"
    assert rows[0]["developer"] == "patrick"
    assert rows[0]["before"] == {"ram_avail_gb": 22.7}


@pytest.mark.asyncio
async def test_record_restart_attributes_to_the_active_session(wired, monkeypatch):
    async with _direct(wired) as c:
        sid = (await c.post("/api/sessions", json={
            "developer": "patrick", "scope": []})).json()["id"]
    monkeypatch.setattr(mcp, "load_session_id", lambda: sid)

    await mcp.call_tool("record_restart", {"unit": "anime-studio", "reason": "deploy"})
    async with _direct(wired) as c:
        rows = (await c.get("/api/restarts")).json()
    assert rows[0]["session_id"] == sid


@pytest.mark.asyncio
async def test_a_stale_session_pointer_does_not_lose_the_record(wired, monkeypatch):
    """A reaped session leaves a stale pointer; the restart still happened.

    Losing the record here would be the worst case -- the sessions most likely to
    hold a stale pointer are long-running ones, exactly those that restart things.
    """
    monkeypatch.setattr(mcp, "load_session_id", lambda: "reaped-session-id")
    out = await mcp.call_tool("record_restart", {"unit": "comfyui", "reason": "recycle"})
    assert "Recorded restart" in out[0].text

    async with _direct(wired) as c:
        rows = (await c.get("/api/restarts")).json()
    assert len(rows) == 1
    assert rows[0]["session_id"] is None
    assert rows[0]["developer"] == "patrick", "falls back to the git user"


@pytest.mark.asyncio
async def test_team_status_surfaces_a_recent_restart(wired):
    """THE point of the ticket: a peer session can see the bounce without asking."""
    async with _direct(wired) as c:
        await c.post("/api/sessions", json={"developer": "patrick", "scope": ["src/**"]})
        await c.post("/api/restarts", json={
            "unit": "comfyui", "developer": "patrick",
            "reason": "reclaiming VRAM before the flux bench"})

    text = (await mcp.call_tool("team_status", {}))[0].text
    assert "comfyui" in text, "a restart must be visible in team_status, not just stored"
    assert "reclaiming VRAM" in text, "the REASON is the part a decision count destroyed"
    assert "ago" in text


@pytest.mark.asyncio
async def test_team_status_flags_a_failed_restart(wired):
    async with _direct(wired) as c:
        await c.post("/api/sessions", json={"developer": "patrick", "scope": []})
        await c.post("/api/restarts", json={
            "unit": "comfyui-rocm", "outcome": "failed", "reason": "unit is masked"})
    text = (await mcp.call_tool("team_status", {}))[0].text
    assert "FAILED" in text, "a service that did not come back is the urgent case"


@pytest.mark.asyncio
async def test_team_status_is_unchanged_when_nothing_was_restarted(wired):
    async with _direct(wired) as c:
        await c.post("/api/sessions", json={"developer": "patrick", "scope": []})
    text = (await mcp.call_tool("team_status", {}))[0].text
    assert "restarted recently" not in text, "no restarts must add no noise"


@pytest.mark.asyncio
async def test_team_status_survives_a_server_without_the_endpoint(wired, monkeypatch):
    """An older ats-server must not break team_status -- the tool sessions rely on
    to see each other at all. Fail silent and empty, never raise."""
    async def boom(*a, **k):
        raise RuntimeError("404 / connection refused")

    async with _direct(wired) as c:
        await c.post("/api/sessions", json={"developer": "patrick", "scope": []})

    class _Client:
        async def get(self, *a, **k):
            await boom()

    block = await mcp._recent_restarts_block(_Client())
    assert block == ""


@pytest.mark.asyncio
async def test_an_old_restart_falls_out_of_the_team_status_window(wired):
    """A bounce matters to a peer while its effects are in play, not forever."""
    async with _direct(wired) as c:
        await c.post("/api/sessions", json={"developer": "patrick", "scope": []})
        await c.post("/api/restarts", json={"unit": "comfyui", "reason": "ancient history"})

    class _Aged:
        async def get(self, url, params=None, **k):
            async with _direct(wired) as c:
                resp = await c.get("/api/restarts", params=params)
            rows = resp.json()
            for r in rows:
                r["age_seconds"] = mcp.RESTART_WINDOW_SECONDS + 60

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return rows

            return _R()

    assert await mcp._recent_restarts_block(_Aged()) == ""


@pytest.mark.asyncio
async def test_recent_restarts_distinguishes_none_recorded_from_none_happened(wired):
    """An empty history is not evidence that nothing was restarted.

    Restarts done outside ATS leave no trace, and reading "no restarts" as "the
    service was never bounced" is exactly the false-absence conclusion that wastes
    a debugging session.
    """
    text = (await mcp.call_tool("recent_restarts", {}))[0].text
    assert "none were RECORDED" in text


@pytest.mark.asyncio
async def test_recent_restarts_filters_by_unit(wired):
    async with _direct(wired) as c:
        for unit in ("comfyui", "anime-studio"):
            await c.post("/api/restarts", json={"unit": unit, "reason": f"bounced {unit}"})

    text = (await mcp.call_tool("recent_restarts", {"unit": "comfyui"}))[0].text
    assert "comfyui" in text
    assert "anime-studio" not in text


@pytest.mark.parametrize("seconds,expected", [
    (5, "5s ago"), (600, "10m ago"), (7200, "2h ago"), (200000, "2d ago"), (None, "0s ago"),
])
def test_age_formatting(seconds, expected):
    assert mcp._fmt_age(seconds) == expected
