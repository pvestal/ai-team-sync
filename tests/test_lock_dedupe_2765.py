"""#2765: one session, one live row per lane.

POST /api/locks used to exclude the caller's own rows from its overlap check and
then INSERT unconditionally, so a session could hold one pattern several times
with contradictory modes. These pin the chosen semantics:

- re-claiming a lane the session holds LIVE is refused 409 `lane_already_held`,
  naming the existing row; nothing is written;
- an EXPIRED own row never blocks and is never returned: the re-claim gets a new
  row with a fresh TTL, and the expired twin is replaced, not left beside it;
- an authority-bearing row is never rewritten or removed by this door;
- MCP extend_scope reports an already-held lane as a non-fatal no-op;
- the de-duplicate script defaults to a counted dry run that changes nothing.
"""
from __future__ import annotations

import importlib.util
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.models import Base, ScopeLock, Session
from ai_team_sync.server import create_app

pytestmark = pytest.mark.asyncio

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_scope_locks.py"


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


async def _session(client, scope=None, auto_lock=False, **extra) -> str:
    resp = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code", "scope": scope or [],
        "auto_lock": auto_lock, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _rows(db_session, sid, pattern=None):
    db_session.expire_all()
    q = select(ScopeLock).where(ScopeLock.session_id == sid)
    if pattern is not None:
        q = q.where(ScopeLock.pattern == pattern)
    return (await db_session.execute(q)).scalars().all()


async def _expire(db_session, lock_id):
    await db_session.execute(update(ScopeLock).where(ScopeLock.id == lock_id).values(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db_session.commit()


# ── create_lock semantics ─────────────────────────────────────────────────


async def test_reclaiming_a_live_own_lane_is_refused_and_writes_nothing(client, db_session):
    sid = await _session(client)
    first = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**", "mode": "exclusive", "reason": "first"})
    assert first.status_code == 201, first.text
    held = first.json()

    for mode in ("advisory", "exclusive"):
        again = await client.post("/api/locks", json={
            "session_id": sid, "pattern": "src/**", "mode": mode, "reason": "again"})
        assert again.status_code == 409, again.text
        detail = again.json()["detail"]
        assert detail["error"] == "lane_already_held"
        assert detail["lock_id"] == held["id"]
        assert detail["mode"] == "exclusive"
        assert detail["requested_mode"] == mode
        assert detail["expires_at"]
        assert held["id"] in detail["message"] and "exclusive" in detail["message"]

    rows = await _rows(db_session, sid, "src/**")
    assert [(r.id, r.mode, r.reason) for r in rows] == [(held["id"], "exclusive", "first")]
    board = [lk for lk in (await client.get("/api/locks")).json() if lk["session_id"] == sid]
    assert len(board) == 1


async def test_a_spelling_of_the_same_lane_is_the_same_lane(client, db_session):
    sid = await _session(client)
    assert (await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**"})).status_code == 201
    again = await client.post("/api/locks", json={"session_id": sid, "pattern": "./src/**"})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error"] == "lane_already_held"
    assert len(await _rows(db_session, sid)) == 1


async def test_other_lanes_and_other_sessions_are_unaffected(client, db_session):
    a = await _session(client)
    b = await _session(client)
    assert (await client.post("/api/locks", json={"session_id": a, "pattern": "src/**"})).status_code == 201
    # A different pattern in the same session is a different lane.
    assert (await client.post("/api/locks", json={"session_id": a, "pattern": "docs/**"})).status_code == 201
    # Another session's advisory claim on the same pattern still shares, as before.
    assert (await client.post("/api/locks", json={"session_id": b, "pattern": "src/**"})).status_code == 201
    assert len(await _rows(db_session, a)) == 2
    assert len(await _rows(db_session, b)) == 1


@pytest.mark.parametrize("mode", ["advisory", "exclusive"])
async def test_an_expired_own_row_never_blocks_and_is_replaced_with_a_fresh_ttl(
        client, db_session, mode):
    sid = await _session(client)
    first = (await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**", "mode": mode})).json()
    await _expire(db_session, first["id"])

    again = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**", "mode": mode, "reason": "re-claim"})
    assert again.status_code == 201, again.text
    body = again.json()
    assert body["id"] != first["id"], "the dead row must never be handed back"
    assert _aware(datetime.fromisoformat(body["expires_at"])) > \
        datetime.now(timezone.utc) + timedelta(minutes=30), "a re-claim gets a fresh TTL"

    rows = await _rows(db_session, sid, "src/**")
    assert [r.id for r in rows] == [body["id"]], "the expired twin is replaced, not kept beside it"
    board = [lk for lk in (await client.get("/api/locks")).json() if lk["session_id"] == sid]
    assert [lk["id"] for lk in board] == [body["id"]]


async def test_a_refused_reclaim_over_an_expired_row_leaves_that_row_alone(client, db_session):
    """The expired twin is only replaced when the new claim is granted."""
    me = await _session(client)
    mine = (await client.post("/api/locks", json={
        "session_id": me, "pattern": "src/**", "mode": "advisory"})).json()
    await _expire(db_session, mine["id"])
    other = await _session(client)
    assert (await client.post("/api/locks", json={
        "session_id": other, "pattern": "src/**", "mode": "exclusive"})).status_code == 201

    refused = await client.post("/api/locks", json={
        "session_id": me, "pattern": "src/**", "mode": "advisory"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "scope_conflict"
    assert [r.id for r in await _rows(db_session, me)] == [mine["id"]]


async def test_a_live_authority_bearing_row_is_never_rewritten(client, db_session):
    sid = await _session(client)
    bearing = ScopeLock(session_id=sid, pattern="src/**", mode="exclusive",
                        reason="creation claim", authority_bearing=True)
    db_session.add(bearing)
    await db_session.commit()
    before = (bearing.id, bearing.mode, bearing.reason, _aware(bearing.expires_at))

    resp = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**", "mode": "advisory", "reason": "downgrade?"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "lane_already_held"
    assert detail["lock_id"] == bearing.id
    assert "authority" in detail["message"]

    rows = await _rows(db_session, sid)
    assert len(rows) == 1
    row = rows[0]
    assert (row.id, row.mode, row.reason, _aware(row.expires_at)) == before
    assert row.authority_bearing is True


async def test_an_expired_authority_bearing_row_is_left_untouched_by_a_reclaim(client, db_session):
    """This door never writes or removes an authority-bearing row: the new row is
    a coordinating row (authority_bearing False) and the old one stays as it was."""
    sid = await _session(client)
    bearing = ScopeLock(session_id=sid, pattern="src/**", mode="advisory",
                        reason="creation claim", authority_bearing=True,
                        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    db_session.add(bearing)
    await db_session.commit()

    resp = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**", "mode": "exclusive"})
    assert resp.status_code == 201, resp.text
    rows = {r.id: r for r in await _rows(db_session, sid)}
    assert set(rows) == {bearing.id, resp.json()["id"]}
    assert rows[bearing.id].authority_bearing is True
    assert (rows[bearing.id].mode, rows[bearing.id].reason) == ("advisory", "creation claim")
    assert rows[resp.json()["id"]].authority_bearing is False


# ── extend_scope ──────────────────────────────────────────────────────────


@pytest.fixture
def mcp_app(db_engine, monkeypatch):
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
    return lambda: AsyncClient(transport=transport, base_url="http://localhost:8400")


async def test_extend_scope_on_an_already_held_lane_is_a_non_fatal_no_op(
        mcp_app, monkeypatch, db_session):
    async with mcp_app() as c:
        sid = await _session(c, scope=["src/a/**"], auto_lock=True)
    monkeypatch.setattr(mcp, "load_session_id", lambda: sid)

    text = (await mcp.call_tool("extend_scope", {"patterns": ["src/a/**"]}))[0].text
    assert "NOT extended" not in text and "refused" not in text.lower(), text
    assert "already held" in text.lower()
    assert "src/a/**" in text
    assert len(await _rows(db_session, sid, "src/a/**")) == 1

    # Mixed with a new lane: the new lane is taken, the held one reported, no error.
    text = (await mcp.call_tool("extend_scope", {"patterns": ["src/a/**", "src/b/**"]}))[0].text
    assert "Scope extended (+1 advisory lock(s))" in text, text
    assert "already held" in text.lower()
    assert "refused" not in text.lower()
    async with mcp_app() as c:
        assert set((await c.get(f"/api/sessions/{sid}")).json()["scope"]) == {"src/a/**", "src/b/**"}
    assert len(await _rows(db_session, sid, "src/a/**")) == 1
    assert len(await _rows(db_session, sid, "src/b/**")) == 1


async def test_extend_scope_asking_another_mode_says_the_mode_was_not_changed(
        mcp_app, monkeypatch, db_session):
    async with mcp_app() as c:
        sid = await _session(c, scope=["src/a/**"], auto_lock=True)
    monkeypatch.setattr(mcp, "load_session_id", lambda: sid)

    text = (await mcp.call_tool("extend_scope", {"patterns": ["src/a/**"], "mode": "exclusive"}))[0].text
    assert "already held" in text.lower()
    assert "mode not changed" in text.lower()
    assert "delete_lock" in text
    rows = await _rows(db_session, sid, "src/a/**")
    assert [r.mode for r in rows] == ["advisory"]


# ── the de-duplicate script ───────────────────────────────────────────────


def _load_script():
    spec = importlib.util.spec_from_file_location("dedupe_scope_locks_2765", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed_duplicates(url: str) -> dict:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    live, dead = now + timedelta(hours=4), now - timedelta(hours=1)
    async with factory() as db:
        s1 = Session(developer="patrick", agent="claude-code", repo_root="/repo",
                     last_heartbeat=now)
        s2 = Session(developer="patrick", agent="codex", repo_root="/repo",
                     last_heartbeat=now)
        db.add_all([s1, s2])
        await db.flush()
        rows = {
            # s1 src/**: the measured stack (exclusive, advisory, exclusive) plus
            # an expired advisory twin. Keeper = the exclusive with the latest expiry.
            "s1_ex_early": ScopeLock(session_id=s1.id, pattern="src/**", mode="exclusive",
                                     expires_at=live - timedelta(hours=1)),
            "s1_adv": ScopeLock(session_id=s1.id, pattern="src/**", mode="advisory",
                                expires_at=live + timedelta(hours=1)),
            "s1_ex_late": ScopeLock(session_id=s1.id, pattern="./src/**", mode="exclusive",
                                    expires_at=live),
            "s1_dead": ScopeLock(session_id=s1.id, pattern="src/**", mode="advisory",
                                 expires_at=dead),
            # s1 docs/**: an authority-bearing advisory row and a coordinating
            # advisory twin. The bearing row is never removed; the twin goes.
            "s1_docs_bearing": ScopeLock(session_id=s1.id, pattern="docs/**", mode="advisory",
                                         expires_at=live, authority_bearing=True),
            "s1_docs_twin": ScopeLock(session_id=s1.id, pattern="docs/**", mode="advisory",
                                      expires_at=live + timedelta(hours=1)),
            # s2 src/**: one row, not a duplicate of s1's (another session).
            "s2_single": ScopeLock(session_id=s2.id, pattern="src/**", mode="advisory",
                                   expires_at=live),
        }
        db.add_all(rows.values())
        await db.commit()
        ids = {name: row.id for name, row in rows.items()}
    await engine.dispose()
    return ids


async def _all_ids(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(select(ScopeLock.id))
        ids = {r[0] for r in result}
    await engine.dispose()
    return ids


async def test_dedupe_dry_run_counts_correctly_and_changes_nothing(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}"
    ids = await _seed_duplicates(url)
    before = await _all_ids(url)
    script = _load_script()

    out = io.StringIO()
    report = await script.run(url, apply=False, out=out)
    removed = {r["id"] for r in report["remove"]}
    assert removed == {ids["s1_ex_early"], ids["s1_adv"], ids["s1_dead"], ids["s1_docs_twin"]}
    assert report["groups"] == 2
    assert {r["id"] for r in report["keep"]} == {ids["s1_ex_late"], ids["s1_docs_bearing"]}
    text = out.getvalue()
    assert "DRY RUN" in text and "4 row(s)" in text
    for rid in removed:
        assert rid in text
    assert await _all_ids(url) == before, "a dry run changes nothing"

    # The exit code reports what was found, so it can gate a deploy.
    assert await script.main_async(["--database-url", url]) == 1
    assert await _all_ids(url) == before


async def test_dedupe_apply_removes_only_the_planned_rows(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}"
    ids = await _seed_duplicates(url)
    script = _load_script()

    report = await script.run(url, apply=True, out=io.StringIO())
    assert len(report["remove"]) == 4
    assert await _all_ids(url) == {ids["s1_ex_late"], ids["s1_docs_bearing"], ids["s2_single"]}

    # Idempotent: a second pass finds nothing.
    again = await script.run(url, apply=False, out=io.StringIO())
    assert again["remove"] == [] and again["groups"] == 0
    assert await script.main_async(["--database-url", url]) == 0
