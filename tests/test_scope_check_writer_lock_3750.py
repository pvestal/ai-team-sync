"""#3750: the scope-conflict check must not compute file overlaps while it holds
the SQLite writer lock.

Measured 2026-11-15 on the live server: POST /api/locks and start_session run
_check_scope_conflicts inside acquisition_guard (BEGIN IMMEDIATE). It compared
every claimed pattern with every live lock against file indexes of 278-worktree
repositories and issued one override query per live lock, holding the whole-file
writer lock for seconds (3.40 s observed). Every writer outside the guard -- a
heartbeat UPDATE, a file-activity INSERT, a delegation INSERT -- waited the
driver's 5 s busy timeout and failed with `database is locked` (HTTP 500).

These tests reproduce that on a file database with a second connection standing
in for the heartbeat: while the acquisition decides overlap, that writer must get
through within a short busy timeout. They also pin the in-guard SQL to a count
that does not grow with the number of live locks.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync import repo_files
from ai_team_sync.database import get_db
from ai_team_sync.models import Base, OverrideRequest, ScopeLock, Session
from ai_team_sync.routers.override_requests import (approved_override_for_lock,
                                                    approved_override_lock_ids)
from ai_team_sync.server import create_app
from tests.test_ownership_enforcement_3581 import _make_repo

HOLDER_FILES = tuple(f"pkg/f{i}.py" for i in range(12))
HEARTBEAT_BUDGET_S = 0.5  # the stand-in writer's busy timeout


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """The app on a real SQLite FILE, so a second connection meets its locks."""
    path = tmp_path / "ats.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, engine, str(path)
    await engine.dispose()


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path / "repo", HOLDER_FILES + ("README.md",))


async def _session(client, root, scope=()):
    resp = await client.post("/api/sessions", json={
        "developer": "p", "agent": f"claude-code:{uuid.uuid4().hex[:8]}",
        "scope": list(scope), "auto_lock": bool(scope), "repo_root": root})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _holders(client, root, n):
    return [await _session(client, root, [HOLDER_FILES[i]]) for i in range(n)]


def _heartbeat_from_another_connection(db_path: str, session_id: str) -> str:
    """The production victim's statement, on its own connection, with a short
    busy timeout. 'ok' or the driver's error text."""
    conn = sqlite3.connect(db_path, timeout=HEARTBEAT_BUDGET_S)
    try:
        conn.execute("UPDATE sessions SET last_heartbeat = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), session_id))
        conn.commit()
        return "ok"
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        conn.close()


@pytest.fixture
def heartbeat_during_overlap(monkeypatch):
    """Every time the acquisition computes a pattern overlap, a heartbeat from
    another connection tries to land. Returns the outcomes."""
    outcomes: list[str] = []
    target: dict[str, str] = {}
    real = repo_files.patterns_overlap

    def probing(*args, **kwargs):
        if target:
            outcomes.append(_heartbeat_from_another_connection(target["db"], target["sid"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(repo_files, "patterns_overlap", probing)

    def arm(db_path: str, session_id: str) -> list[str]:
        outcomes.clear()
        target.update(db=db_path, sid=session_id)
        return outcomes

    return arm


@pytest.mark.asyncio
async def test_a_heartbeat_lands_while_post_locks_decides_overlap(
        file_db, repo, heartbeat_during_overlap):
    client, _engine, path = file_db
    holders = await _holders(client, repo, 6)
    requester = await _session(client, repo)
    outcomes = heartbeat_during_overlap(path, holders[0])

    resp = await client.post("/api/locks", json={
        "session_id": requester, "pattern": "pkg/**", "mode": "advisory"})

    assert resp.status_code == 201, resp.text
    assert len(outcomes) >= len(holders), "the check must compare against every holder"
    assert set(outcomes) == {"ok"}, (
        f"a writer outside the guard was locked out while overlap was decided: {outcomes}")


@pytest.mark.asyncio
async def test_a_heartbeat_lands_while_start_session_decides_overlap(
        file_db, repo, heartbeat_during_overlap):
    client, _engine, path = file_db
    holders = await _holders(client, repo, 6)
    outcomes = heartbeat_during_overlap(path, holders[0])

    await _session(client, repo, ["pkg/**", "README.md"])

    assert len(outcomes) >= len(holders)
    assert set(outcomes) == {"ok"}, outcomes


@pytest.mark.asyncio
async def test_the_conflict_answer_is_unchanged(file_db, repo):
    """Moving the computation out of the lock must not change what is refused:
    an exclusive claim still meets every overlapping holder, the 409 names them
    all, and a non-overlapping claim is still granted."""
    client, _engine, _path = file_db
    holders = await _holders(client, repo, 3)
    requester = await _session(client, repo)
    resp = await client.post("/api/locks", json={
        "session_id": requester, "pattern": "pkg/**", "mode": "exclusive"})
    assert resp.status_code == 409, resp.text
    assert {c["session_id"] for c in resp.json()["detail"]["conflicts"]} == set(holders)
    resp = await client.post("/api/locks", json={
        "session_id": requester, "pattern": "README.md", "mode": "exclusive"})
    assert resp.status_code == 201, resp.text


@pytest.fixture
def lock_lands_after_warm_up(monkeypatch):
    """Commit a conflicting EXCLUSIVE lock from another connection right after
    the warm-up read the board, i.e. before the guard is taken."""
    from ai_team_sync.routers import sessions as sessions_router

    real = sessions_router.warm_scope_conflicts
    plan: dict = {}

    async def warm_then_race(*args, **kwargs):
        await real(*args, **kwargs)
        if plan:
            async with plan["factory"]() as other:
                other.add(ScopeLock(session_id=plan["holder"], pattern=plan["pattern"],
                                    mode="exclusive", reason="race", authority_bearing=False))
                await other.commit()
            plan.clear()

    monkeypatch.setattr(sessions_router, "warm_scope_conflicts", warm_then_race)
    return plan


@pytest.mark.asyncio
async def test_a_lock_committed_after_the_warm_up_is_still_refused(
        file_db, repo, lock_lands_after_warm_up):
    """The memo only answers what it was asked; the guard re-reads the board, so
    a lane taken between the warm-up and the guard is computed there and wins."""
    client, engine, _path = file_db
    late_holder = await _session(client, repo)
    requester = await _session(client, repo)
    lock_lands_after_warm_up.update(
        factory=async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        holder=late_holder, pattern="pkg/f5.py")

    resp = await client.post("/api/locks", json={
        "session_id": requester, "pattern": "pkg/**", "mode": "advisory"})

    assert not lock_lands_after_warm_up, "the race lock was never committed"
    assert resp.status_code == 409, resp.text
    assert {c["session_id"] for c in resp.json()["detail"]["conflicts"]} == {late_holder}


@pytest.mark.asyncio
async def test_start_session_refuses_a_lock_committed_after_the_warm_up(
        file_db, repo, lock_lands_after_warm_up):
    client, engine, _path = file_db
    late_holder = await _session(client, repo)
    lock_lands_after_warm_up.update(
        factory=async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        holder=late_holder, pattern="pkg/f7.py")

    resp = await client.post("/api/sessions", json={
        "developer": "p", "agent": f"claude-code:{uuid.uuid4().hex[:8]}",
        "scope": ["pkg/**"], "auto_lock": True, "repo_root": repo})

    assert not lock_lands_after_warm_up, "the race lock was never committed"
    assert resp.status_code == 409, resp.text


def _count_in_guard(engine):
    """SQL statements between BEGIN IMMEDIATE and the commit that ends it."""
    state = {"open": False, "counts": []}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _stmt(conn, cursor, statement, *a):
        if statement.strip().upper().startswith("BEGIN IMMEDIATE"):
            state["open"] = True
            state["counts"].append(0)
        elif state["open"]:
            state["counts"][-1] += 1

    @event.listens_for(engine.sync_engine, "commit")
    def _commit(conn):
        state["open"] = False

    @event.listens_for(engine.sync_engine, "rollback")
    def _rollback(conn):
        state["open"] = False

    state["hooks"] = (_stmt, _commit, _rollback)
    return state


@pytest.mark.asyncio
async def test_in_guard_sql_does_not_grow_with_live_locks(file_db, repo):
    """One override read for all candidates, not one per live lock: the guarded
    statement count is the same with 3 live locks as with 12."""
    client, engine, _path = file_db
    counted, made = [], 0
    for n_holders, pattern in ((3, "pkg/**"), (12, "pkg/*.py")):
        for i in range(made, n_holders):
            await _session(client, repo, [HOLDER_FILES[i]])
        made = n_holders
        requester = await _session(client, repo)
        state = _count_in_guard(engine)
        resp = await client.post("/api/locks", json={
            "session_id": requester, "pattern": pattern, "mode": "advisory"})
        assert resp.status_code == 201, resp.text
        assert len(state["counts"]) == 1, state
        counted.append(state["counts"][0])
        event.remove(engine.sync_engine, "before_cursor_execute", state["hooks"][0])
        event.remove(engine.sync_engine, "commit", state["hooks"][1])
        event.remove(engine.sync_engine, "rollback", state["hooks"][2])
    assert counted[0] == counted[1], counted


# ── the batched override read answers exactly what the per-lock one does ──

@pytest_asyncio.fixture
async def db(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.mark.asyncio
async def test_batched_override_read_matches_the_per_lock_rule(db):
    now = datetime.now(timezone.utc)
    requester, owner, other_owner, stranger = (str(uuid.uuid4()) for _ in range(4))
    for sid in (requester, owner, other_owner, stranger):
        db.add(Session(id=sid, developer="p", agent="a", status="active", scope="[]"))
    old = now - timedelta(hours=2)
    locks = {
        "approved": ScopeLock(session_id=owner, pattern="a/**", mode="exclusive",
                              created_at=old),
        # The approval predates this lock: recreating a lock does not inherit it.
        "recreated": ScopeLock(session_id=owner, pattern="b/**", mode="exclusive",
                               created_at=now - timedelta(minutes=1)),
        "expired": ScopeLock(session_id=owner, pattern="c/**", mode="exclusive",
                             created_at=old),
        "pending": ScopeLock(session_id=owner, pattern="d/**", mode="exclusive",
                             created_at=old),
        "other_owner": ScopeLock(session_id=other_owner, pattern="a/**",
                                 mode="exclusive", created_at=old),
        "other_pattern": ScopeLock(session_id=owner, pattern="e/**", mode="exclusive",
                                   created_at=old),
        "stranger_asked": ScopeLock(session_id=owner, pattern="f/**", mode="exclusive",
                                    created_at=old),
        "two_rows_one_good": ScopeLock(session_id=owner, pattern="g/**",
                                       mode="exclusive", created_at=old),
    }
    for lock in locks.values():
        db.add(lock)

    def grant(pattern, *, status="approved", created=None, expires=None, who=requester,
              to=owner):
        db.add(OverrideRequest(
            requester_session_id=who, owner_session_id=to, conflicting_pattern=pattern,
            justification="t", status=status,
            created_at=created or now - timedelta(minutes=30),
            expires_at=expires or now + timedelta(minutes=30)))

    grant("a/**")
    grant("b/**", created=now - timedelta(minutes=30))   # before the lock was recreated
    grant("c/**", expires=now - timedelta(minutes=1))
    grant("d/**", status="pending")
    grant("x/**")                                        # names no live lock
    grant("f/**", who=stranger)
    grant("g/**", created=old - timedelta(hours=1))      # predates the lock
    grant("g/**")                                        # and a valid one
    await db.commit()

    rows = list(locks.values())
    expected = {lock.id for lock in rows if await approved_override_for_lock(db, requester, lock)}
    assert expected == {locks["approved"].id, locks["two_rows_one_good"].id}
    assert await approved_override_lock_ids(db, requester, rows) == expected
    assert await approved_override_lock_ids(db, "", rows) == set()
    assert await approved_override_lock_ids(db, requester, []) == set()
