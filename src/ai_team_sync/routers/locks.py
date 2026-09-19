"""Scope lock management endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync import peer_identity
from ai_team_sync.database import get_db
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.scope_paths import reader_covers, reader_lock, reader_query
from ai_team_sync.schemas import (
    LockCheckRequest,
    LockCheckResult,
    LockMatch,
    LockCreate,
    LockResponse,
)

router = APIRouter(prefix="/locks", tags=["locks"])


def _lock_to_response(lock: ScopeLock, developer: str | None = None,
                      agent: str | None = None,
                      repo_root: str = "") -> LockResponse:
    return LockResponse(
        id=lock.id,
        session_id=lock.session_id,
        pattern=lock.pattern,
        reason=lock.reason or "",
        mode=lock.mode,
        created_at=lock.created_at,
        expires_at=lock.expires_at,
        developer=developer,
        agent=agent,
        repo_root=repo_root,
    )


async def _get_active_locks(db: AsyncSession) -> list[tuple[ScopeLock, Session, str]]:
    """Return all live locks with their OWNING SESSION and that session's
    repo_root ('' = unanchored legacy session).

    The owner is carried, not just its developer name: a verdict reader has to
    know WHICH SESSION holds a lock to leave the caller's own out (#2757), and
    the shared human name cannot answer that.

    Live = unexpired, or EXCLUSIVE and held by a live owner (#2741): the TTL
    sweep keeps those, and a lock that still blocks a mutation grant must not be
    invisible to the board and to every other client's conflict check.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScopeLock, Session, Session.developer, Session.repo_root)
        .join(Session)
        .where(or_(ScopeLock.expires_at > now, ScopeLock.mode == "exclusive"))
        .where(Session.status.in_(["active", "paused"]))
    )
    return [(lock, owner, repo_root) for lock, owner, developer, repo_root in result.all()
            if _aware(lock.expires_at) > now or live_exclusive_owner(owner)]


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def live_exclusive_owner(owner: Session) -> bool:
    """Whether `owner` still holds its exclusive locks past their TTL: active,
    or paused and not silent past the heartbeat window."""
    if owner.status == "active":
        return True
    return owner.status == "paused" and not _owner_is_stale(owner)


def _cross_repo(caller_repo_root: str, lock_repo_root: str) -> bool:
    """A lock's repo-relative pattern only means something inside its own repo.
    Skip the lock when BOTH sides are anchored and to different repos; if either
    side is unanchored (''), fall back to legacy match-everywhere behavior
    (conservative — never lose protection during the transition)."""
    a = (caller_repo_root or "").rstrip("/")
    b = (lock_repo_root or "").rstrip("/")
    return bool(a) and bool(b) and a != b


@router.post("", response_model=LockResponse, status_code=201)
async def create_lock(body: LockCreate, request: Request, db: AsyncSession = Depends(get_db)):
    # Verify session exists and is active
    result = await db.execute(select(Session).where(Session.id == body.session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status not in ("active", "paused"):
        raise HTTPException(400, "Session is not active")
    if cross_account(peer_identity.peer_uid_for_request(request), session):
        raise HTTPException(403, detail={
            "error": "session_not_yours",
            "message": (f"session {session.id} belongs to another OS account; "
                        f"locks are attached to a session by the account that owns it"),
            "session_id": session.id})

    # Session creation's overlap rule, applied to every lock (#2756).
    from ai_team_sync.routers.sessions import (
        _check_scope_conflicts, blocking_conflict, scope_conflict_detail)

    conflicts = await _check_scope_conflicts(
        db, [body.pattern], session.developer, repo_root=session.repo_root or "",
        exclude_session_id=session.id, requester_session_id=session.id)
    conflict = blocking_conflict(conflicts, body.mode)
    if conflict is not None:
        raise HTTPException(409, detail=scope_conflict_detail("lock", conflict, conflicts))

    # A lock made here coordinates; it never bears authority (#2741). A mutation
    # grant is measured only against a bound session's creation-time claims, so
    # a caller cannot manufacture the claim that would authorize it.
    lock = ScopeLock(
        session_id=body.session_id, pattern=body.pattern, mode=body.mode, reason=body.reason,
        authority_bearing=False,
    )
    db.add(lock)
    await db.commit()
    await db.refresh(lock)
    return _lock_to_response(lock, developer=session.developer, agent=session.agent,
                             repo_root=session.repo_root or "")


@router.get("", response_model=list[LockResponse])
async def list_locks(db: AsyncSession = Depends(get_db)):
    locks = await _get_active_locks(db)
    return [_lock_to_response(lock, developer=owner.developer, agent=owner.agent,
                              repo_root=owner.repo_root or "")
            for lock, owner, _root in locks]


@router.post("/check", response_model=list[LockCheckResult])
async def check_locks(body: LockCheckRequest, request: Request,
                      db: AsyncSession = Depends(get_db)):
    """Which live lock covers each path. Lexical, per docs/lock-readers.md: each
    path and each lock is placed once per request, then compared with fnmatch."""
    from ai_team_sync.caller_session import resolve_caller_session
    from ai_team_sync.routers.override_requests import approved_override_for_lock

    caller = await resolve_caller_session(db, request=request, session_id=body.session_id)
    active_locks = await _get_active_locks(db)
    results = []

    locks = [(lock, owner, reader_lock(lock.pattern, lock_repo_root))
             for lock, owner, lock_repo_root in active_locks]
    for path in body.paths:
        query = reader_query(path, body.repo_root)
        hits = [(lock, owner) for lock, owner, form in locks if reader_covers(query, form)]
        if not hits:
            results.append(LockCheckResult(
                path=path, locked=False, caller_identity_unresolved=caller.unresolved))
            continue
        matches = []
        for lock, owner in hits:
            matches.append(LockMatch(
                lock_id=lock.id, session_id=lock.session_id, agent=owner.agent,
                developer=owner.developer, mode=lock.mode, pattern=lock.pattern,
                reason=lock.reason or "", is_own=caller.owns(lock),
                override_granted=await approved_override_for_lock(
                    db, caller.session_id or "", lock)))
        # Keep the legacy top-level fields, choosing a foreign exclusive claim first.
        lock, owner = next((h for h in hits if h[0].mode == "exclusive"
                            and not caller.owns(h[0])),
                           next((h for h in hits if not caller.owns(h[0])), hits[0]))
        results.append(LockCheckResult(
            path=path,
            locked=True,
            lock_id=lock.id,
            session_id=lock.session_id,
            developer=owner.developer,
            agent=owner.agent,
            mode=lock.mode,
            pattern=lock.pattern,
            reason=lock.reason or "",
            matches=matches,
            caller_identity_unresolved=caller.unresolved,
        ))

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


def cross_account(peer_uid: int | None, owner: Session, *, any_status: bool = False) -> bool:
    """Is a request from `peer_uid` reaching into a LIVE session another OS
    account created? (#2741) With any_status=True, into such a session in any
    status — for callers whose ownership outlives the session being live.

    A session id is not a secret — team_status lists them — so naming one proves
    nothing. The creating account is the one owner fact the kernel vouches for.

    No staleness exception across accounts. A session that merely went quiet
    for the heartbeat window was reproduced being completed, re-anchored or
    stripped of its exclusive lock by another account, which was then granted
    the file. Another account's ghost is the in-process reaper's to collect;
    a same-account ghost stays reapable exactly as before.

    Owner never identified (rows older than #2741): an unidentifiable requester
    or a headless bound account is refused, because a bound worker can make
    itself unidentifiable on purpose (a forwarding header, a socket closed
    before the lookup). Ordinary identified accounts keep the old behaviour.
    """
    if not any_status and owner.status not in ("active", "paused"):
        return False
    creator = getattr(owner, "creator_uid", None)
    if creator is not None:
        return peer_uid != creator
    from ai_team_sync.workers import registry

    return peer_uid is None or peer_uid in registry().bound_account_uids()


@router.delete("/{lock_id}", status_code=204)
async def delete_lock(lock_id: str, request: Request, actor_session_id: str = "",
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
    if owner is not None and cross_account(peer_identity.peer_uid_for_request(request), owner):
        raise HTTPException(
            403,
            detail={
                "error": "lock_not_yours",
                "message": (f"lock {lock.id} belongs to a live session of another OS "
                            f"account; naming its session id does not make it yours."),
                "owner_session_id": lock.session_id,
            })
    await db.delete(lock)
    await db.commit()
