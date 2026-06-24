"""Background tasks for ai-team-sync server."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.config import settings
from ai_team_sync.database import get_db
from ai_team_sync.models import CommitRecord, Decision, OverrideRequest, ScopeLock, Session
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


async def auto_complete_stale_sessions(db: AsyncSession) -> int:
    """Auto-complete 'active' sessions with no activity for session_inactivity_hours.

    Activity = the most recent of: session.started_at, its newest lock/commit/decision.
    No new schema needed — activity is derived from existing timestamps. Conservative
    window (default 12h) so genuinely-active work is never closed; this only clears
    phantom-active sessions an agent forgot to complete (the failure mode where stale
    sessions linger on the board and hold their lane).
    """
    def _aware(dt):
        # SQLite returns tz-naive datetimes even for timezone=True columns; assume UTC.
        return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.session_inactivity_hours)
    result = await db.execute(select(Session).where(Session.status == "active"))
    completed = 0
    for sess in result.scalars().all():
        last_lock = await db.scalar(
            select(func.max(ScopeLock.created_at)).where(ScopeLock.session_id == sess.id))
        last_commit = await db.scalar(
            select(func.max(CommitRecord.created_at)).where(CommitRecord.session_id == sess.id))
        last_decision = await db.scalar(
            select(func.max(Decision.created_at)).where(Decision.session_id == sess.id))
        last_activity = max(
            _aware(t) for t in (sess.started_at, last_lock, last_commit, last_decision) if t)
        if last_activity < cutoff:
            sess.status = "completed"
            sess.completed_at = now
            note = f"[auto-completed: inactive >{settings.session_inactivity_hours}h]"
            sess.summary = f"{sess.summary} {note}".strip() if sess.summary else note
            await broadcast_event(sess.id, "session.auto_completed", {
                "session_id": sess.id, "last_activity": last_activity.isoformat()})
            completed += 1
    if completed:
        await db.commit()
    return completed


async def cleanup_task_loop():
    """Background loop: sweep expired locks + stale override-requests + stale sessions."""
    while True:
        try:
            async for db in get_db():
                await check_expired_locks(db)
                await expire_stale_override_requests(db)
                await auto_complete_stale_sessions(db)
        except Exception as e:
            print(f"Error in cleanup task: {e}")

        # Wait 1 minute before next check
        await asyncio.sleep(60)


async def start_background_tasks():
    """Start all background tasks."""
    # Start cleanup loop in background
    asyncio.create_task(cleanup_task_loop())
