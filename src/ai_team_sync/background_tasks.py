"""Background tasks for ai-team-sync server."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import OverrideRequest, ScopeLock, Session
from ai_team_sync.events import broadcast_event


async def check_expired_locks(db: AsyncSession) -> int:
    """Notify and DELETE expired locks. Returns the number swept.

    Rewritten 2026-06-24: the old version (a) had a real bug — it filtered with
    ``datetime.replace(minute=now.minute - 1)`` which raises ValueError at minute 0
    and is logically meaningless — and (b) only broadcast an event, never deleting
    the lock. Expired locks therefore accumulated as DB cruft forever. Now every
    lock with ``expires_at <= now`` is notified once and removed, so the board
    reflects reality and stale locks stop lingering (up to the 8h TTL was the
    symptom behind 'why are there stale locks').
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScopeLock, Session).join(Session).where(ScopeLock.expires_at <= now)
    )
    expired = list(result.all())
    for lock, session in expired:
        await broadcast_event(session.id, "lock.expired", {
            "lock_id": lock.id,
            "pattern": lock.pattern,
            "mode": lock.mode,
            "expired_at": lock.expires_at.isoformat(),
        })
        await db.delete(lock)
    if expired:
        await db.commit()
    return len(expired)


async def expire_stale_override_requests(db: AsyncSession) -> int:
    """Flip pending override-requests past their response window to 'expired'.

    The model sets a 15-min ``expires_at`` but nothing ever enforced it, so a
    requester could wait forever on a dead request. Returns the number expired.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.status == "pending")
        .where(OverrideRequest.expires_at <= now)
    )
    stale = list(result.scalars().all())
    for req in stale:
        req.status = "expired"
        req.responded_at = now
        await broadcast_event(req.requester_session_id, "override.expired", {
            "request_id": req.id,
            "conflicting_pattern": req.conflicting_pattern,
        })
    if stale:
        await db.commit()
    return len(stale)


async def cleanup_task_loop():
    """Background loop: sweep expired locks + stale override-requests every minute."""
    while True:
        try:
            async for db in get_db():
                await check_expired_locks(db)
                await expire_stale_override_requests(db)
        except Exception as e:
            print(f"Error in cleanup task: {e}")

        # Wait 1 minute before next check
        await asyncio.sleep(60)


async def start_background_tasks():
    """Start all background tasks."""
    # Start cleanup loop in background
    asyncio.create_task(cleanup_task_loop())
