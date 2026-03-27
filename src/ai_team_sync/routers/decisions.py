"""Decision log endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import Decision, Session
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.schemas import DecisionCreate, DecisionResponse

router = APIRouter(prefix="/decisions", tags=["decisions"])


def _decision_to_response(d: Decision) -> DecisionResponse:
    return DecisionResponse(
        id=d.id,
        session_id=d.session_id,
        title=d.title,
        chosen=d.chosen,
        rejected=d.rejected,
        reasoning=d.reasoning,
        files=json.loads(d.files) if d.files else [],
        created_at=d.created_at,
    )


@router.post("", response_model=DecisionResponse, status_code=201)
async def create_decision(body: DecisionCreate, db: AsyncSession = Depends(get_db)):
    # Verify session exists
    result = await db.execute(select(Session).where(Session.id == body.session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    decision = Decision(
        session_id=body.session_id,
        title=body.title,
        chosen=body.chosen,
        rejected=body.rejected,
        reasoning=body.reasoning,
        files=json.dumps(body.files),
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)

    await dispatch("decision.logged", {
        "developer": session.developer,
        "title": decision.title,
        "chosen": decision.chosen,
        "rejected": decision.rejected,
        "session_id": session.id,
    })

    return _decision_to_response(decision)


@router.get("", response_model=list[DecisionResponse])
async def list_decisions(
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Decision).order_by(Decision.created_at.desc())
    if session_id:
        query = query.where(Decision.session_id == session_id)
    result = await db.execute(query)
    return [_decision_to_response(d) for d in result.scalars().all()]


@router.get("/{decision_id}", response_model=DecisionResponse)
async def get_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Decision).where(Decision.id == decision_id))
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(404, "Decision not found")
    return _decision_to_response(decision)
