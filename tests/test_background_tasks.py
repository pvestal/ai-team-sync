"""Cleanup-task sweep: expired locks are DELETED (not just notified) and stale
pending override-requests flip to 'expired'. Regression guard for the rewrite that
fixed the minute-0 ValueError + the never-deletes bug (2026-06-24).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.background_tasks import (
    auto_complete_stale_sessions,
    check_expired_locks,
    expire_stale_override_requests,
)
from ai_team_sync.config import settings
from ai_team_sync.models import OverrideRequest, ScopeLock, Session


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_check_expired_locks_deletes_expired_keeps_live(db_session):
    sess = Session(developer="patrick", agent="claude-code")
    db_session.add(sess)
    await db_session.flush()

    live = ScopeLock(session_id=sess.id, pattern="src/live/**",
                     expires_at=_utcnow() + timedelta(hours=1))
    dead = ScopeLock(session_id=sess.id, pattern="src/dead/**",
                     expires_at=_utcnow() - timedelta(minutes=1))
    db_session.add_all([live, dead])
    await db_session.commit()

    swept = await check_expired_locks(db_session)
    assert swept == 1

    remaining = (await db_session.execute(select(ScopeLock))).scalars().all()
    patterns = {l.pattern for l in remaining}
    assert patterns == {"src/live/**"}  # dead deleted, live kept


@pytest.mark.asyncio
async def test_check_expired_locks_no_minute_zero_crash(db_session):
    # The old impl did datetime.replace(minute=now.minute-1) -> ValueError at :00.
    # A clean run with nothing expired must simply return 0, never raise.
    assert await check_expired_locks(db_session) == 0


@pytest.mark.asyncio
async def test_expire_stale_override_requests(db_session):
    a = Session(developer="patrick")
    b = Session(developer="patrick")
    db_session.add_all([a, b])
    await db_session.flush()

    fresh = OverrideRequest(requester_session_id=a.id, owner_session_id=b.id,
                            conflicting_pattern="src/**",
                            expires_at=_utcnow() + timedelta(minutes=10))
    stale = OverrideRequest(requester_session_id=a.id, owner_session_id=b.id,
                            conflicting_pattern="src/**",
                            expires_at=_utcnow() - timedelta(minutes=1))
    db_session.add_all([fresh, stale])
    await db_session.commit()

    n = await expire_stale_override_requests(db_session)
    assert n == 1

    rows = (await db_session.execute(select(OverrideRequest))).scalars().all()
    by_status = sorted(r.status for r in rows)
    assert by_status == ["expired", "pending"]


@pytest.mark.asyncio
async def test_auto_complete_stale_session_but_keep_recent(db_session):
    h = settings.session_inactivity_hours
    stale = Session(developer="patrick",
                    started_at=_utcnow() - timedelta(hours=h + 1))   # idle past window
    recent = Session(developer="patrick",
                     started_at=_utcnow() - timedelta(minutes=5))    # fresh
    db_session.add_all([stale, recent])
    await db_session.commit()

    n = await auto_complete_stale_sessions(db_session)
    assert n == 1

    await db_session.refresh(stale)
    await db_session.refresh(recent)
    assert stale.status == "completed"
    assert stale.completed_at is not None
    assert "auto-completed" in (stale.summary or "")
    assert recent.status == "active"


@pytest.mark.asyncio
async def test_stale_heartbeat_reaped_fast(db_session):
    # A session that HEARTBEATED but went silent past the fast window is reaped
    # well before the 12h fallback — this is the dead-process fast path.
    m = settings.session_heartbeat_timeout_minutes
    sess = Session(developer="patrick",
                   started_at=_utcnow() - timedelta(minutes=m + 30),
                   last_heartbeat=_utcnow() - timedelta(minutes=m + 5))
    db_session.add(sess)
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1
    await db_session.refresh(sess)
    assert sess.status == "completed"
    assert "heartbeat lost" in (sess.summary or "")


@pytest.mark.asyncio
async def test_fresh_heartbeat_keeps_session_alive(db_session):
    # Old start, but a recent heartbeat = provably alive -> never reaped, even though
    # it is far past the fast window's start time.
    m = settings.session_heartbeat_timeout_minutes
    sess = Session(developer="patrick",
                   started_at=_utcnow() - timedelta(hours=20),
                   last_heartbeat=_utcnow() - timedelta(minutes=max(1, m // 4)))
    db_session.add(sess)
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 0
    await db_session.refresh(sess)
    assert sess.status == "active"


@pytest.mark.asyncio
async def test_never_heartbeated_uses_slow_window(db_session):
    # A session that never heartbeated and is silent for longer than the FAST window
    # but well within the 12h fallback must NOT be reaped — the fast path only applies
    # once last_heartbeat is set, so legacy clients are unaffected (never-worse).
    m = settings.session_heartbeat_timeout_minutes
    sess = Session(developer="patrick",
                   started_at=_utcnow() - timedelta(minutes=m + 30))  # no heartbeat
    db_session.add(sess)
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 0
    await db_session.refresh(sess)
    assert sess.status == "active"


@pytest.mark.asyncio
async def test_recent_lock_keeps_old_session_alive(db_session):
    h = settings.session_inactivity_hours
    sess = Session(developer="patrick",
                   started_at=_utcnow() - timedelta(hours=h + 5))  # old start...
    db_session.add(sess)
    await db_session.flush()
    # ...but a fresh lock = recent activity, so it must NOT be auto-completed
    db_session.add(ScopeLock(session_id=sess.id, pattern="src/**",
                             created_at=_utcnow() - timedelta(minutes=2)))
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 0
    await db_session.refresh(sess)
    assert sess.status == "active"
