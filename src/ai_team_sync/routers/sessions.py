"""Session CRUD endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_team_sync.database import get_db
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.schemas import SessionCreate, SessionResponse, SessionUpdate

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


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)):
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
        for pattern in body.scope:
            lock = ScopeLock(session_id=session.id, pattern=pattern)
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
