"""#2811 — an ordinary heartbeat racing a reaper sweep must never leave the
response and the row disagreeing.

THE RACE. `auto_complete_stale_sessions` reads active rows with a plain SELECT
and later writes `status='completed'` unconditionally; `heartbeat_session`
branches on the status it ALREADY LOADED and later writes `last_heartbeat`
unconditionally. Neither write checks what the other committed in between, so:

  H-stale  heartbeat loads A (active) -> reaper reaps A and commits -> heartbeat
           stamps the corpse and answers 200 "active" while the row is
           completed with its locks deleted.
  R-stale  reaper loads A (silent) -> heartbeat commits a fresh stamp -> reaper
           reaps a session that has just proved it is alive.

HOW THE INTERLEAVING IS FORCED. A FILE-backed SQLite (the production shape:
every AsyncSession gets its own pooled connection, unlike the shared in-memory
fixture) and an AsyncSession subclass that runs a hook after its FIRST
statement returns. For both handlers that statement is the load, before any
write, in the old code and the fixed code alike, so the hook lands exactly in
the window the ticket describes and no write lock is held while it runs.

WHAT IS ASSERTED. Structured fields only: response status / lock_count /
restoration_outcome against the persisted row and its ScopeLock rows.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.database import get_db
from ai_team_sync.models import Base, ScopeLock, Session
from ai_team_sync.server import create_app

ROOT = "/srv/ats-2811"
OWNER_TOKEN = "test-owner-capability-2811"
PATTERN = "src/raced.py"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _long_ago() -> datetime:
    return _utcnow() - timedelta(hours=settings.session_inactivity_hours + 24)


class _HookedSession(AsyncSession):
    """Runs `after_first_statement` once, right after the first execute()
    returns. Nothing is flushed or written by then, so the hook may commit on
    another connection without meeting this session's write lock."""

    after_first_statement = None

    async def execute(self, *args, **kwargs):
        result = await super().execute(*args, **kwargs)
        hook, self.after_first_statement = self.after_first_statement, None
        if hook is not None:
            await hook()
        return result


@pytest_asyncio.fixture
async def file_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ats-2811.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def harness(file_engine):
    """(client, factory, arm): `arm(hook)` attaches `hook` to the NEXT request's
    database session only."""
    factory = async_sessionmaker(file_engine, class_=_HookedSession, expire_on_commit=False)
    pending: list = []

    async def override_get_db():
        async with factory() as session:
            if pending:
                session.after_first_statement = pending.pop()
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, pending.append


async def _silent_session_with_lock(factory) -> str:
    async with factory() as db:
        sess = Session(developer="pvestal", agent="claude-code:owner-2811", repo_root=ROOT,
                       scope=json.dumps([PATTERN]), status="active",
                       creator_uid=os.getuid(),
                       approval_token_hash=hashlib.sha256(OWNER_TOKEN.encode()).hexdigest(),
                       started_at=_long_ago(), last_heartbeat=_long_ago())
        db.add(sess)
        await db.flush()
        db.add(ScopeLock(session_id=sess.id, pattern=PATTERN, mode="advisory",
                         reason="2811", created_at=_long_ago(),
                         expires_at=_utcnow() + timedelta(hours=4)))
        await db.commit()
        return sess.id


async def _persisted(factory, session_id) -> tuple[Session, list[str]]:
    async with factory() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
        locks = (await db.execute(
            select(ScopeLock.pattern).where(ScopeLock.session_id == session_id))).scalars().all()
        return row, list(locks)


async def _reap(factory) -> int:
    async with factory() as db:
        return await auto_complete_stale_sessions(db)


# ── positive controls: the harness itself changes nothing ────────────────

async def test_control_unraced_heartbeat_stamps_an_active_session(harness):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    before = _utcnow().replace(tzinfo=None)

    resp = await client.post(f"/api/sessions/{sid}/heartbeat")

    assert resp.status_code == 200, resp.text
    row, locks = await _persisted(factory, sid)
    assert (resp.json()["status"], row.status) == ("active", "active")
    assert row.last_heartbeat >= before
    assert locks == [PATTERN]


async def test_control_unraced_reaper_still_reaps_a_silent_session(harness):
    _, factory, _ = harness
    sid = await _silent_session_with_lock(factory)

    assert await _reap(factory) == 1

    row, locks = await _persisted(factory, sid)
    assert (row.status, row.auto_completed, locks) == ("completed", True, [])
    assert PATTERN in json.loads(row.reaped_locks)["locks"][0]["pattern"]


# ── H-stale: the reaper commits between the heartbeat's load and its write ─

async def test_heartbeat_that_loaded_active_never_answers_200_over_a_reaped_row(harness):
    client, factory, arm = harness
    sid = await _silent_session_with_lock(factory)
    reaped: list[int] = []

    async def reaper_wins_the_gap():
        reaped.append(await _reap(factory))

    arm(reaper_wins_the_gap)
    resp = await client.post(f"/api/sessions/{sid}/heartbeat")

    assert reaped == [1], "the interleaving did not happen; the test proves nothing"
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row, locks = await _persisted(factory, sid)
    # The response and the row agree, and the agent that is demonstrably alive
    # keeps its lane: the heartbeat disproved the reap, so it resurrects.
    assert body["status"] == row.status == "active"
    assert row.auto_completed is False
    assert locks == [PATTERN]
    assert body["lock_count"] == len(locks)
    assert body["restoration_outcome"] != "not_attempted"
    assert PATTERN in body["locks_restored"]


async def test_heartbeat_that_loaded_active_is_refused_when_the_owner_completed_it(harness):
    """Same window, other completer: an OWNER completion committed in the gap.
    A heartbeat must not stamp it or answer 'active' (resurrection is only for
    reaper completions)."""
    client, factory, arm = harness
    sid = await _silent_session_with_lock(factory)

    async def owner_completes_in_the_gap():
        async with factory() as db:
            row = (await db.execute(select(Session).where(Session.id == sid))).scalar_one()
            row.status, row.completed_at, row.auto_completed = "completed", _utcnow(), False
            await db.commit()

    arm(owner_completes_in_the_gap)
    resp = await client.post(f"/api/sessions/{sid}/heartbeat")

    row, _ = await _persisted(factory, sid)
    assert row.status == "completed"
    assert resp.status_code == 409, resp.text
    assert _aware(row.last_heartbeat) < _utcnow() - timedelta(hours=1), \
        "a refused heartbeat stamped the completed row"


# ── R-stale: a heartbeat commits between the reaper's load and its write ──

async def test_reaper_that_loaded_a_silent_row_does_not_reap_after_a_fresh_heartbeat(harness):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    beats: list[int] = []

    async def heartbeat_wins_the_gap():
        beats.append((await client.post(f"/api/sessions/{sid}/heartbeat")).status_code)

    async with factory() as db:
        db.after_first_statement = heartbeat_wins_the_gap
        reaped = await auto_complete_stale_sessions(db)
        # A lost CAS is a zero-row UPDATE, which still opens a write
        # transaction. With the sweep's session STILL OPEN, another writer
        # must get through at once rather than wait out the busy timeout.
        async with factory() as other:
            await asyncio.wait_for(other.execute(
                update(Session).where(Session.id == sid).values(description="2811")), 2)
            await other.commit()

    assert beats == [200], "the interleaving did not happen; the test proves nothing"
    row, locks = await _persisted(factory, sid)
    assert reaped == 0
    assert (row.status, row.auto_completed) == ("active", False)
    assert locks == [PATTERN]
    assert not row.reaped_locks


# ── unforced concurrency: the invariant under real thread interleavings ──

@pytest.mark.parametrize("round_", range(20))
async def test_concurrent_heartbeat_and_sweep_never_disagree(harness, round_):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)

    resp, _ = await asyncio.gather(
        client.post(f"/api/sessions/{sid}/heartbeat"), _reap(factory))

    row, locks = await _persisted(factory, sid)
    if resp.status_code == 200 and resp.json()["status"] == "active":
        assert row.status == "active", f"200 'active' over a {row.status} row"
        assert locks == [PATTERN]
    assert resp.status_code != 500, resp.text


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt
