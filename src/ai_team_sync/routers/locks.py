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
        reason=lock.reason or "",
        mode=lock.mode,
        created_at=lock.created_at,
        expires_at=lock.expires_at,
        developer=developer,
    )


async def _get_active_locks(db: AsyncSession) -> list[tuple[ScopeLock, str, str]]:
    """Return all non-expired locks with their developer names and the owning
    session's repo_root ('' = unanchored legacy session)."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScopeLock, Session.developer, Session.repo_root)
        .join(Session)
        .where(ScopeLock.expires_at > now)
        .where(Session.status.in_(["active", "paused"]))
    )
    return list(result.all())


def _cross_repo(caller_repo_root: str, lock_repo_root: str) -> bool:
    """A lock's repo-relative pattern only means something inside its own repo.
    Skip the lock when BOTH sides are anchored and to different repos; if either
    side is unanchored (''), fall back to legacy match-everywhere behavior
    (conservative — never lose protection during the transition)."""
    a = (caller_repo_root or "").rstrip("/")
    b = (lock_repo_root or "").rstrip("/")
    return bool(a) and bool(b) and a != b


@router.post("", response_model=LockResponse, status_code=201)
async def create_lock(body: LockCreate, db: AsyncSession = Depends(get_db)):
    # Verify session exists and is active
    result = await db.execute(select(Session).where(Session.id == body.session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status not in ("active", "paused"):
        raise HTTPException(400, "Session is not active")

    lock = ScopeLock(
        session_id=body.session_id, pattern=body.pattern, mode=body.mode, reason=body.reason
    )
    db.add(lock)
    await db.commit()
    await db.refresh(lock)
    return _lock_to_response(lock, developer=session.developer)


@router.get("", response_model=list[LockResponse])
async def list_locks(db: AsyncSession = Depends(get_db)):
    locks = await _get_active_locks(db)
    return [_lock_to_response(lock, developer=dev) for lock, dev, _root in locks]


@router.post("/check", response_model=list[LockCheckResult])
async def check_locks(body: LockCheckRequest, db: AsyncSession = Depends(get_db)):
    """Check if any of the given paths conflict with active locks."""
    active_locks = await _get_active_locks(db)
    results = []

    for path in body.paths:
        matched = False
        for lock, developer, lock_repo_root in active_locks:
            if _cross_repo(body.repo_root, lock_repo_root):
                continue  # pattern belongs to a different repo — not a conflict here
            if fnmatch(path, lock.pattern):
                results.append(LockCheckResult(
                    path=path,
                    locked=True,
                    lock_id=lock.id,
                    session_id=lock.session_id,
                    developer=developer,
                    mode=lock.mode,
                    pattern=lock.pattern,
                    reason=lock.reason or "",
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


def _owner_is_stale(owner: Session) -> bool:
    """A GHOST still marked active — silent past the heartbeat window.

    Completing a session releases its locks, so a lock whose owner is properly
    finished is already gone. The lock that actually needs reaping belongs to a
    session that LOOKS active and is not, which is what list_all_locks surfaces
    lock ids for. Keep that path open; a genuinely live session heartbeats.
    """
    from datetime import datetime, timezone
    from ai_team_sync.config import settings

    def _aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt

    last = _aware(owner.last_heartbeat) or _aware(owner.started_at)
    if last is None:
        return False
    idle = (datetime.now(timezone.utc) - last).total_seconds()
    return idle > settings.session_heartbeat_timeout_minutes * 60


@router.delete("/{lock_id}", status_code=204)
async def delete_lock(lock_id: str, actor_session_id: str = "",
                      db: AsyncSession = Depends(get_db)):
    """Owner-bound, with the reap path kept open.

    A lock held by a LIVE session is that session's claim and nobody else's to
    drop. A lock left behind by a session that is no longer active is exactly
    what delete_lock exists to clear, so that stays allowed — reaping a corpse's
    lane is the documented use. An actor that does not identify itself can still
    reap a dead lock and still cannot touch a live one.
    """
    result = await db.execute(select(ScopeLock).where(ScopeLock.id == lock_id))
    lock = result.scalar_one_or_none()
    if not lock:
        raise HTTPException(404, "Lock not found")

    owner = await db.get(Session, lock.session_id)
    if owner is not None and owner.status == "active" and not _owner_is_stale(owner) \
            and actor_session_id != lock.session_id:
        raise HTTPException(
            403,
            detail={
                "error": "lock_not_yours",
                "message": (f"lock {lock.id} is held by ACTIVE session "
                            f"{lock.session_id} ({owner.agent}). Coordinate or "
                            f"request_override; a live claim is not reapable."),
                "owner_session_id": lock.session_id,
            })
    await db.delete(lock)
    await db.commit()
