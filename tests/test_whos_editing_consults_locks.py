"""whos_editing must not say "Clear to go" on presence alone (2026-08-25).

/api/presence/check answers ONE question -- who has hands on the file in the
last few minutes -- and its own docstring says so: "Live presence only --
declared scope locks are a separate check (/locks/check)." The MCP tool took
that single answer and rendered "✅ Nobody else is editing these files right
now. Clear to go."

IT FIRED FOR REAL, TWICE, IN ONE EVENING.
  * whos_editing returned clear for
    packages/scene_generation/scene_generation_ops_routes_regenerate.py while
    session 0fd3a304 held an advisory lock on that exact path. The caller took
    the clearance, restarted anime-studio, and shipped that file's uncommitted
    working-tree contents to production on behalf of an author it had not
    identified.
  * A second session got the same clear for clip_depth_transfer.py against two
    live locks held by the first.
The live presence store was [] in both cases while /api/locks/check returned
locked=true for the same path.

Presence is the WEAKER signal: it can be empty for reasons the caller cannot
see. Locks are DECLARED and durable. A tool whose answer gates an edit has to
consult the durable one, and must not convert a failure to check it into
silence.

Same defect family as the check_git_changes false all-clear and the
pre_commit_check wiring mismatch: a coordination surface reporting a clearance
it never established.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app

PATH = "packages/scene_generation/regenerate.py"


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


async def _other_session_locking(c: AsyncClient, path: str) -> str:
    r = await c.post("/api/sessions", json={
        "developer": "pvestal", "agent": "claude-code:other",
        "scope": [path], "auto_lock": True, "lock_mode": "advisory"})
    r.raise_for_status()
    return r.json()["id"]


@pytest.mark.asyncio
async def test_a_locked_path_is_never_reported_clear(db_engine, monkeypatch):
    """The exact 2026-08-25 failure: presence empty, lock held, tool said clear."""
    transport = _wire(monkeypatch, db_engine)
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        await _other_session_locking(c, PATH)
        out = await mcp.call_tool("whos_editing", {"paths": [PATH]})

    text = out[0].text
    assert "Clear to go" not in text, (
        "a path under an active scope lock was reported clear")
    assert PATH in text
    assert "lock" in text.lower()


@pytest.mark.asyncio
async def test_the_lock_holder_is_named(db_engine, monkeypatch):
    """'Someone holds it' is not actionable; the caller has to know who to ask.
    Not knowing the owner is precisely what caused the bad deploy."""
    transport = _wire(monkeypatch, db_engine)
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _other_session_locking(c, PATH)
        out = await mcp.call_tool("whos_editing", {"paths": [PATH]})

    text = out[0].text
    assert "pvestal" in text
    assert sid[:8] in text, "the holding session is not identified"


@pytest.mark.asyncio
async def test_quiet_is_not_reported_as_free(db_engine, monkeypatch):
    """A lock with NO live presence is the dangerous case — it looks like
    nothing is happening. The output must say so in words."""
    transport = _wire(monkeypatch, db_engine)
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        await _other_session_locking(c, PATH)
        out = await mcp.call_tool("whos_editing", {"paths": [PATH]})

    low = out[0].text.lower()
    assert "still means an owner" in low or "do not read the quiet as free" in low


@pytest.mark.asyncio
async def test_a_genuinely_free_path_is_still_cleared(db_engine, monkeypatch):
    """The all-clear must remain reachable, or the test above is satisfied by a
    tool that simply never clears anything."""
    transport = _wire(monkeypatch, db_engine)
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        out = await mcp.call_tool("whos_editing", {"paths": ["src/untouched.py"]})

    text = out[0].text
    assert "Clear to go" in text
    assert "no scope lock" in text.lower(), (
        "the all-clear does not state that locks were actually checked")


@pytest.mark.asyncio
async def test_only_the_locked_path_is_flagged(db_engine, monkeypatch):
    transport = _wire(monkeypatch, db_engine)
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        await _other_session_locking(c, PATH)
        out = await mcp.call_tool(
            "whos_editing", {"paths": [PATH, "src/elsewhere.py"]})

    assert "Clear to go" not in out[0].text
    assert PATH in out[0].text


@pytest.mark.asyncio
async def test_a_failing_lock_check_degrades_to_unknown_not_clear(
        db_engine, monkeypatch):
    """The failure direction matters more than the happy path. If the lock check
    cannot run, the answer is a NAMED unknown -- never silence that reads as
    permission."""
    transport = _wire(monkeypatch, db_engine)

    real_post = AsyncClient.post

    async def flaky_post(self, url, *a, **k):
        if "locks/check" in str(url):
            raise RuntimeError("lock service unreachable")
        return await real_post(self, url, *a, **k)

    monkeypatch.setattr(AsyncClient, "post", flaky_post)
    out = await mcp.call_tool("whos_editing", {"paths": [PATH]})

    text = out[0].text
    assert "Clear to go" not in text, (
        "a failed lock check was rendered as clearance")
    assert "check_locks" in text
