"""Shared-service restart records (#2559).

ATS coordinates FILES (scope_locks) and WORK (tower tasks). Restarting a shared
service was governed only by convention in CLAUDE.md and recorded nowhere, so a
bounce was invisible to every other session until something broke.

This router RECORDS restarts; it does not gate them. Refusal against a claimed unit
is a deliberately separate decision -- a guard that makes an emergency recycle
harder than going out-of-band would be worse than no guard at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import ServiceRestart, Session
from ai_team_sync.schemas import (
    RestartCreate,
    RestartResponse,
    RestartUpdate,
    normalize_unit,
)

router = APIRouter(prefix="/restarts", tags=["restarts"])


def _loads(raw: str) -> dict:
    """Tolerate a malformed blob rather than 500 the whole history.

    These payloads are free-form and written by shell one-liners; one bad row must
    not make every other restart unreadable.
    """
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _age_seconds(created_at) -> float:
    """Seconds since the restart, tolerating a naive timestamp.

    SQLite persists no UTC offset, so a row read back carries tzinfo=None even
    though the column is DateTime(timezone=True). Values are written by _utcnow(),
    so a naive value is UTC and is treated as such rather than as local time.
    """
    if created_at is None:
        return 0.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())


def _to_response(r: ServiceRestart) -> RestartResponse:
    return RestartResponse(
        age_seconds=_age_seconds(r.created_at),
        id=r.id,
        unit=r.unit,
        session_id=r.session_id,
        developer=r.developer or "",
        reason=r.reason or "",
        outcome=r.outcome,
        old_pid=r.old_pid,
        new_pid=r.new_pid,
        before=_loads(r.before_state),
        after=_loads(r.after_state),
        created_at=r.created_at,
    )


@router.post("", response_model=RestartResponse, status_code=201)
async def record_restart(body: RestartCreate, db: AsyncSession = Depends(get_db)):
    developer = body.developer or ""
    if body.session_id:
        result = await db.execute(select(Session).where(Session.id == body.session_id))
        session = result.scalar_one_or_none()
        if not session:
            # Matches create_lock. A dangling id would make the record unattributable,
            # which defeats the point of recording it.
            raise HTTPException(404, "Session not found")
        developer = developer or session.developer

    restart = ServiceRestart(
        unit=body.unit,  # already normalized by RestartCreate
        session_id=body.session_id,
        developer=developer,
        reason=body.reason,
        outcome=body.outcome,
        old_pid=body.old_pid,
        new_pid=body.new_pid,
        before_state=json.dumps(body.before or {}),
        after_state=json.dumps(body.after or {}),
    )
    db.add(restart)
    await db.commit()
    await db.refresh(restart)
    return _to_response(restart)


@router.get("", response_model=list[RestartResponse])
async def list_restarts(
    unit: str = Query("", description="filter to one unit; normalized like the writer"),
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Recent restarts, newest first -- 'when was this last bounced, and by whom'."""
    stmt = select(ServiceRestart).order_by(ServiceRestart.created_at.desc()).limit(limit)
    if unit:
        # Normalize the QUERY side too, or 'comfyui.service' would fail to find rows
        # stored as 'comfyui' and the caller would conclude it was never restarted.
        stmt = stmt.where(ServiceRestart.unit == normalize_unit(unit))
    result = await db.execute(stmt)
    return [_to_response(r) for r in result.scalars().all()]


@router.patch("/{restart_id}", response_model=RestartResponse)
async def update_restart(
    restart_id: str, body: RestartUpdate, db: AsyncSession = Depends(get_db)
):
    """Attach the outcome and the after-measurement once the unit has settled."""
    result = await db.execute(select(ServiceRestart).where(ServiceRestart.id == restart_id))
    restart = result.scalar_one_or_none()
    if not restart:
        raise HTTPException(404, "Restart not found")

    if body.outcome is not None:
        restart.outcome = body.outcome
    if body.after is not None:
        restart.after_state = json.dumps(body.after)
    if body.reason is not None:
        restart.reason = body.reason
    if body.new_pid is not None:
        restart.new_pid = body.new_pid
    # `unit`, `before_state` and `created_at` are deliberately immutable: they are
    # what happened, and a later edit would quietly rewrite the evidence.

    await db.commit()
    await db.refresh(restart)
    return _to_response(restart)
