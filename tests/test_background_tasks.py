"""Cleanup-task sweep: expired locks are DELETED (not just notified) and stale
pending override-requests flip to 'expired'. Regression guard for the rewrite that
fixed the minute-0 ValueError + the never-deletes bug (2026-06-24).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.background_tasks import check_expired_locks, expire_stale_override_requests
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
