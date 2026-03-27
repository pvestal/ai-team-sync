"""Background tasks for ai-team-sync server."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.events import broadcast_event


async def check_expired_locks(db: AsyncSession):
    """Check for expired locks and notify sessions."""
    now = datetime.now(timezone.utc)

    # Find locks that just expired
    result = await db.execute(
        select(ScopeLock, Session)
        .join(Session)
        .where(ScopeLock.expires_at <= now)
        .where(ScopeLock.expires_at > datetime.now(timezone.utc).replace(minute=datetime.now(timezone.utc).minute - 1))  # Last minute
    )

    expired = list(result.all())

    for lock, session in expired:
        # Notify session that lock expired
        await broadcast_event(session.id, "lock.expired", {
            "lock_id": lock.id,
            "pattern": lock.pattern,
            "mode": lock.mode,
            "expired_at": lock.expires_at.isoformat(),
        })


async def cleanup_task_loop():
    """Background loop to check for expired locks every minute."""
    while True:
        try:
            async for db in get_db():
                await check_expired_locks(db)
        except Exception as e:
            print(f"Error in cleanup task: {e}")

        # Wait 1 minute before next check
        await asyncio.sleep(60)


async def start_background_tasks():
    """Start all background tasks."""
    # Start cleanup loop in background
    asyncio.create_task(cleanup_task_loop())
