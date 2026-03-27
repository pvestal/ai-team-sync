"""Scope lock management endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatch

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.schemas import (
    LockCheckRequest,
    LockCheckResult,
    LockCreate,
    LockResponse,
)

router = APIRouter(prefix="/locks", tags=["locks"])


def _lock_to_response(lock: ScopeLock, developer: str | None = None) -> LockResponse:
    return LockResponse(
        id=lock.id,
        session_id=lock.session_id,
        pattern=lock.pattern,
        mode=lock.mode,
        created_at=lock.created_at,
        expires_at=lock.expires_at,
        developer=developer,
    )


async def _get_active_locks(db: AsyncSession) -> list[tuple[ScopeLock, str]]:
    """Return all non-expired locks with their developer names."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScopeLock, Session.developer)
        .join(Session)
        .where(ScopeLock.expires_at > now)
        .where(Session.status.in_(["active", "paused"]))
    )
    return list(result.all())


@router.post("", response_model=LockResponse, status_code=201)
async def create_lock(body: LockCreate, db: AsyncSession = Depends(get_db)):
    # Verify session exists and is active
    result = await db.execute(select(Session).where(Session.id == body.session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status not in ("active", "paused"):
        raise HTTPException(400, "Session is not active")

    lock = ScopeLock(session_id=body.session_id, pattern=body.pattern, mode=body.mode)
    db.add(lock)
    await db.commit()
    await db.refresh(lock)
    return _lock_to_response(lock, developer=session.developer)


@router.get("", response_model=list[LockResponse])
async def list_locks(db: AsyncSession = Depends(get_db)):
    locks = await _get_active_locks(db)
    return [_lock_to_response(lock, developer=dev) for lock, dev in locks]


@router.post("/check", response_model=list[LockCheckResult])
async def check_locks(body: LockCheckRequest, db: AsyncSession = Depends(get_db)):
    """Check if any of the given paths conflict with active locks."""
    active_locks = await _get_active_locks(db)
    results = []

    for path in body.paths:
        matched = False
        for lock, developer in active_locks:
            if fnmatch(path, lock.pattern):
                results.append(LockCheckResult(
                    path=path,
                    locked=True,
                    lock_id=lock.id,
                    session_id=lock.session_id,
                    developer=developer,
                    mode=lock.mode,
                    pattern=lock.pattern,
                ))
                matched = True
                break
        if not matched:
            results.append(LockCheckResult(path=path, locked=False))

    # Dispatch conflict notifications for any exclusive locks hit
    conflicts = [r for r in results if r.locked and r.mode == "exclusive"]
    if conflicts:
        await dispatch("lock.conflict", {
            "paths": [c.path for c in conflicts],
            "developer": conflicts[0].developer,
            "pattern": conflicts[0].pattern,
        })

    return results


@router.delete("/{lock_id}", status_code=204)
async def delete_lock(lock_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ScopeLock).where(ScopeLock.id == lock_id))
    lock = result.scalar_one_or_none()
    if not lock:
        raise HTTPException(404, "Lock not found")
    await db.delete(lock)
    await db.commit()
