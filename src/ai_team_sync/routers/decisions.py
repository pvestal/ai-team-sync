"""Decision log endpoints."""

from __future__ import annotations

import json
import hashlib
import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import AgentMessage, Decision, Session
from ai_team_sync import peer_identity
from ai_team_sync.routers.locks import cross_account
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.schemas import DecisionCreate, DecisionResponse

router = APIRouter(prefix="/decisions", tags=["decisions"])


def _decision_to_response(d: Decision) -> DecisionResponse:
    return DecisionResponse(
        id=d.id,
        session_id=d.session_id,
        ticket_id=d.ticket_id,
        recipient_session_id=d.recipient_session_id,
        title=d.title,
        chosen=d.chosen,
        rejected=d.rejected,
        reasoning=d.reasoning,
        files=json.loads(d.files) if d.files else [],
        created_at=d.created_at,
    )


class DecisionBody(DecisionCreate):
    # Nested alias carries the session in the PATH; make the body's copy
    # optional so {"title": ...} alone is a valid nested payload (#2517).
    session_id: str | None = None  # type: ignore[assignment]


session_scoped = APIRouter(prefix="/sessions", tags=["decisions"])


@session_scoped.post("/{session_id}/decisions", response_model=DecisionResponse,
                     status_code=201)
async def create_decision_alias(
    session_id: str, body: DecisionBody, request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Alias for POST /api/decisions with session_id in the path (#2517
    failure 2) — the shape agents guess. Delegates to create_decision."""
    return await create_decision(
        DecisionCreate(**{**body.model_dump(exclude_none=True),
                          "session_id": session_id}), request, db)


@router.post("", response_model=DecisionResponse, status_code=201)
async def create_decision(body: DecisionCreate, request: Request,
                          db: AsyncSession = Depends(get_db)):
    # Verify session exists
    result = await db.execute(select(Session).where(Session.id == body.session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    recipient = None
    if body.recipient_session_id:
        if session.status != "active" or cross_account(
                peer_identity.peer_uid_for_request(request), session):
            raise HTTPException(403, "Active sender session is required")
        token = request.headers.get("X-ATS-Approval-Token", "")
        if not token or not hmac.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(),
                session.approval_token_hash or ""):
            raise HTTPException(403, "Sender session capability is required")
        recipient = await db.get(Session, body.recipient_session_id)
        if recipient is None or recipient.status != "active":
            raise HTTPException(409, "Recipient session is not active")
        if recipient.id == session.id:
            raise HTTPException(400, "Recipient must be another session")
    if (body.ticket_id is not None and session.ticket_id is not None
            and session.ticket_id != body.ticket_id):
        raise HTTPException(409, "Decision ticket must match its session ticket")

    decision = Decision(
        session_id=body.session_id,
        ticket_id=body.ticket_id if body.ticket_id is not None else session.ticket_id,
        recipient_session_id=recipient.id if recipient else None,
        title=body.title,
        chosen=body.chosen,
        rejected=body.rejected,
        reasoning=body.reasoning,
        files=json.dumps(body.files),
    )
    db.add(decision)
    await db.flush()
    if recipient:
        db.add(AgentMessage(
            sender_session_id=session.id, recipient_session_id=recipient.id,
            sender_agent=session.agent, sender_developer=session.developer,
            recipient_agent=recipient.agent, ticket_id=decision.ticket_id,
            kind="decision",
            body=(f"Decision {decision.id} — {decision.title}\n"
                  f"Chosen: {decision.chosen}\nReasoning: {decision.reasoning}"),
        ))
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
    repo_root: str | None = None,
    ticket_id: int | None = None,
    since: datetime | None = None,
    limit: int | None = Query(None, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = select(Decision).order_by(Decision.created_at.desc())
    if session_id:
        query = query.where(Decision.session_id == session_id)
    if repo_root:
        query = query.join(Session, Decision.session_id == Session.id).where(
            Session.repo_root == repo_root)
    if ticket_id is not None:
        query = query.where(Decision.ticket_id == ticket_id)
    if since is not None:
        query = query.where(Decision.created_at >= since)
    if limit:
        query = query.limit(limit)
    result = await db.execute(query)
    return [_decision_to_response(d) for d in result.scalars().all()]


@router.get("/{decision_id}", response_model=DecisionResponse)
async def get_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Decision).where(Decision.id == decision_id))
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(404, "Decision not found")
    return _decision_to_response(decision)
