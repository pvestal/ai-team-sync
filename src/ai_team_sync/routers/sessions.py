"""Session CRUD endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from fnmatch import fnmatch

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_team_sync.database import get_db
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.schemas import SessionCreate, SessionResponse, SessionUpdate
from ai_team_sync.config import settings

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _session_to_response(s: Session) -> SessionResponse:
    return SessionResponse(
        id=s.id,
        developer=s.developer,
        agent=s.agent,
        scope=json.loads(s.scope) if s.scope else [],
        description=s.description,
        status=s.status,
        branch=s.branch,
        started_at=s.started_at,
        completed_at=s.completed_at,
        summary=s.summary,
        lock_count=len(s.locks) if s.locks else 0,
        decision_count=len(s.decisions) if s.decisions else 0,
        commit_count=len(s.commits) if s.commits else 0,
    )


async def _check_scope_conflicts(
    db: AsyncSession,
    new_patterns: list[str],
    current_developer: str
) -> list[dict]:
    """Check if new scope patterns conflict with existing active locks."""
    now = datetime.now(timezone.utc)

    # Get all active locks from active sessions
    result = await db.execute(
        select(ScopeLock, Session.developer)
        .join(Session)
        .where(ScopeLock.expires_at > now)
        .where(Session.status.in_(["active", "paused"]))
    )
    active_locks = list(result.all())

    conflicts = []
    for new_pattern in new_patterns:
        for lock, developer in active_locks:
            # Check if patterns overlap using bidirectional matching
            # Pattern A matches Pattern B, or Pattern B matches Pattern A
            if (fnmatch(new_pattern, lock.pattern) or
                fnmatch(lock.pattern, new_pattern) or
                new_pattern == lock.pattern):
                conflicts.append({
                    "new_pattern": new_pattern,
                    "existing_pattern": lock.pattern,
                    "existing_developer": developer,
                    "lock_mode": lock.mode,
                    "session_id": lock.session_id,
                })

    return conflicts


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)):
    # Check for scope conflicts BEFORE creating the session
    if body.auto_lock and body.scope:
        conflicts = await _check_scope_conflicts(db, body.scope, body.developer)

        if conflicts:
            # Determine lock mode for new session
            new_lock_mode = getattr(body, 'lock_mode', settings.lock_default_mode)

            # Separate exclusive vs advisory conflicts
            exclusive_conflicts = [c for c in conflicts if c["lock_mode"] == "exclusive"]

            # Block if:
            # 1. Any existing lock is exclusive, OR
            # 2. User is requesting exclusive mode (can't coexist with any lock)
            if exclusive_conflicts or new_lock_mode == "exclusive":
                conflict = exclusive_conflicts[0] if exclusive_conflicts else conflicts[0]
                mode_msg = (
                    f"exclusive lock '{conflict['existing_pattern']}'"
                    if conflict["lock_mode"] == "exclusive"
                    else f"existing lock '{conflict['existing_pattern']}' (you requested exclusive mode)"
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "scope_conflict",
                        "message": (
                            f"Cannot create session: scope '{conflict['new_pattern']}' conflicts "
                            f"with {mode_msg} held by {conflict['existing_developer']}"
                        ),
                        "conflicts": conflicts,
                    }
                )

            # Advisory conflicts: warn via notification but allow
            for conflict in conflicts:
                await dispatch("lock.conflict", {
                    "new_pattern": conflict["new_pattern"],
                    "existing_pattern": conflict["existing_pattern"],
                    "new_developer": body.developer,
                    "existing_developer": conflict["existing_developer"],
                })

    session = Session(
        developer=body.developer,
        agent=body.agent,
        scope=json.dumps(body.scope),
        description=body.description,
        branch=body.branch,
    )
    db.add(session)
    await db.flush()  # Ensure session.id is populated

    # Auto-create scope locks from scope patterns
    if body.auto_lock and body.scope:
        lock_mode = getattr(body, 'lock_mode', settings.lock_default_mode)
        for pattern in body.scope:
            lock = ScopeLock(session_id=session.id, pattern=pattern, mode=lock_mode)
            db.add(lock)

    await db.commit()

    # Reload with relationships
    result = await db.execute(
        select(Session)
        .where(Session.id == session.id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one()

    await dispatch("session.started", {
        "developer": session.developer,
        "agent": session.agent,
        "scope": body.scope,
        "description": session.description,
        "branch": session.branch,
        "session_id": session.id,
    })

    return _session_to_response(session)


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    status: str | None = None,
    developer: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Session).options(
        selectinload(Session.locks),
        selectinload(Session.decisions),
        selectinload(Session.commits),
    )
    if status:
        query = query.where(Session.status == status)
    if developer:
        query = query.where(Session.developer == developer)
    query = query.order_by(Session.started_at.desc())

    result = await db.execute(query)
    return [_session_to_response(s) for s in result.scalars().all()]


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    return _session_to_response(session)


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(session_id: str, body: SessionUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    if body.status is not None:
        session.status = body.status
        if body.status == "completed":
            session.completed_at = datetime.now(timezone.utc)
            # Release all locks
            for lock in session.locks:
                await db.delete(lock)
    if body.summary is not None:
        session.summary = body.summary
    if body.scope is not None:
        session.scope = json.dumps(body.scope)
    if body.description is not None:
        session.description = body.description

    await db.commit()
    await db.refresh(session)

    if body.status == "completed":
        await dispatch("session.completed", {
            "developer": session.developer,
            "agent": session.agent,
            "branch": session.branch,
            "summary": session.summary or "",
            "session_id": session.id,
        })

    return _session_to_response(session)
