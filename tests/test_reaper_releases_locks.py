"""Reaping a session must free its lane — which the reaper only ever claimed.

auto_complete_stale_sessions carries three separate assertions that it releases
locks:

  :106  "Auto-complete 'active' sessions that have gone silent, so their locks
         don't linger and hold the lane after the agent's process is gone."
  :146  "Reaping drops the session's locks"
  :157  "Holding a dead session's locks is worse than reaping it — the lane
         stays blocked for everyone."

None of it was true. The reaper read max(ScopeLock.created_at) to compute
activity and never deleted a lock. Only two things ever freed one: the expiry
sweep at lock_ttl_hours (8h default), and update_session's completion branch —

    if body.status == "completed":
        session.completed_at = ...
        for lock in session.locks:
            await db.delete(lock)

— which the reaper bypasses by setting sess.status on the model directly. So an
OPERATOR-completed session freed its lane and a REAPED one did not, which is
backwards: reaping is what happens when the agent is actually gone.

Observed 2026-08-25 on the live board: session 9d4793db, auto-completed 4.4
hours earlier, still holding two advisory locks due to expire ~9h later. Its
files read as owned by a process that no longer existed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import ScopeLock, Session


def _long_ago() -> datetime:
    """Well past both the heartbeat and the inactivity window."""
    return datetime.now(timezone.utc) - timedelta(
        hours=settings.session_inactivity_hours + 24)


def _live_expiry() -> datetime:
    """A lock that the expiry sweep would NOT collect — so anything that frees
    it had to be the reaper, not the TTL."""
    return datetime.now(timezone.utc) + timedelta(hours=settings.lock_ttl_hours)


async def _stale_session_holding(db, n_locks: int, *, heartbeated: bool) -> Session:
    sess = Session(developer="patrick", agent="claude-code", status="active",
                   started_at=_long_ago())
    if heartbeated:
        sess.last_heartbeat = _long_ago()
    db.add(sess)
    await db.flush()
    for i in range(n_locks):
        db.add(ScopeLock(session_id=sess.id, pattern=f"src/mod_{i}.py",
                         mode="advisory", created_at=_long_ago(),
                         expires_at=_live_expiry()))
    await db.commit()
    return sess


@pytest.mark.asyncio
async def test_reaping_releases_the_dead_sessions_locks(db_session):
    sess = await _stale_session_holding(db_session, 2, heartbeated=True)

    assert await auto_complete_stale_sessions(db_session) == 1

    held = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sess.id))).scalars().all()
    assert held == [], (
        "a reaped session still holds its locks — the lane stays blocked for "
        "the full lock TTL after the agent is gone")
    await db_session.refresh(sess)
    assert sess.status == "completed"


@pytest.mark.asyncio
async def test_the_locks_were_not_merely_expired(db_session):
    """Guard against a passing-for-the-wrong-reason result: the locks above are
    created with a FUTURE expiry, so the expiry sweep cannot account for them.
    Pinned explicitly because it is the one alternative explanation."""
    sess = await _stale_session_holding(db_session, 1, heartbeated=True)
    lock = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sess.id))).scalar_one()
    assert lock.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)

    await auto_complete_stale_sessions(db_session)

    assert (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sess.id))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_live_sessions_locks_are_left_alone(db_session):
    """The inverse. Releasing locks on reap is only correct if reap is correct:
    an active session that is NOT stale must keep everything."""
    live = Session(developer="patrick", agent="claude-code", status="active",
                   started_at=datetime.now(timezone.utc),
                   last_heartbeat=datetime.now(timezone.utc))
    db_session.add(live)
    await db_session.flush()
    db_session.add(ScopeLock(session_id=live.id, pattern="src/live.py",
                             mode="advisory", expires_at=_live_expiry()))
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 0
    held = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == live.id))).scalars().all()
    assert len(held) == 1, "the sweep took a live session's lock"


@pytest.mark.asyncio
async def test_never_heartbeated_session_also_releases(db_session):
    """The fallback window (legacy clients) must free the lane too — it is the
    path a non-heartbeating agent dies on."""
    sess = await _stale_session_holding(db_session, 3, heartbeated=False)

    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sess.id))).scalars().all() == []


@pytest.mark.asyncio
async def test_one_sessions_reap_does_not_free_anothers_locks(db_session):
    """Scoping check: the delete must be filtered to the session being reaped."""
    dead = await _stale_session_holding(db_session, 2, heartbeated=True)
    live = Session(developer="patrick", agent="claude-code", status="active",
                   started_at=datetime.now(timezone.utc),
                   last_heartbeat=datetime.now(timezone.utc))
    db_session.add(live)
    await db_session.flush()
    db_session.add(ScopeLock(session_id=live.id, pattern="src/other.py",
                             mode="advisory", expires_at=_live_expiry()))
    await db_session.commit()

    await auto_complete_stale_sessions(db_session)

    assert (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == dead.id))).scalars().all() == []
    assert len((await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == live.id))).scalars().all()) == 1
