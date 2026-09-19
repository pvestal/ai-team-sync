"""Durable messages between exact ATS sessions.

Writing, reading, and acknowledging require the actor session's capability.
An HTTP response that listed a message is not a receipt: only an explicit
acknowledgement marks it seen by the recipient agent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync import peer_identity
from ai_team_sync.database import get_db
from ai_team_sync.events import broadcast_event
from ai_team_sync.models import AgentMessage, Handoff, Session
from ai_team_sync.routers.locks import cross_account

router = APIRouter(tags=["messages"])


class MessageCreate(BaseModel):
    sender_session_id: str
    recipient_session_id: str | None = None
    ticket_id: StrictInt | None = Field(default=None, gt=0)
    body: str = Field(min_length=1, max_length=4000)


class MessageAck(BaseModel):
    recipient_session_id: str


class MessageResponse(BaseModel):
    id: str
    sender_session_id: str
    recipient_session_id: str | None
    ticket_id: int | None
    kind: str
    handoff_id: str | None
    sender_agent: str
    sender_developer: str
    recipient_agent: str
    body: str
    created_at: datetime
    acknowledged_at: datetime | None

    model_config = {"from_attributes": True}


async def _actor(session_id: str, request: Request, db: AsyncSession,
                 *, require_active: bool = True) -> Session:
    session = await db.get(Session, session_id)
    if (session is None or (require_active and session.status != "active")
            or cross_account(peer_identity.peer_uid_for_request(request), session)):
        raise HTTPException(403, "Active actor session owned by caller is required")
    token = request.headers.get("X-ATS-Approval-Token", "")
    expected = session.approval_token_hash or ""
    if not token or not expected or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected):
        raise HTTPException(403, "Actor session capability is required")
    return session


@router.post("/messages", response_model=MessageResponse, status_code=201)
async def send_message(body: MessageCreate, request: Request,
                       db: AsyncSession = Depends(get_db)):
    sender = await _actor(body.sender_session_id, request, db)
    if bool(body.recipient_session_id) == bool(body.ticket_id):
        raise HTTPException(422, "Choose one recipient session or next session on a ticket")
    if sender.id == body.recipient_session_id:
        raise HTTPException(400, "Recipient must be another session")
    recipient = None
    if body.recipient_session_id:
        recipient = await db.get(Session, body.recipient_session_id)
        if recipient is None:
            raise HTTPException(404, "Recipient session not found")
        if recipient.status != "active":
            raise HTTPException(409, "Recipient session is not active")
    elif sender.ticket_id != body.ticket_id:
        raise HTTPException(403, "Sender must be linked to the deferred message ticket")
    row = AgentMessage(
        sender_session_id=sender.id,
        recipient_session_id=recipient.id if recipient else None,
        ticket_id=body.ticket_id or (recipient.ticket_id if recipient else None),
        sender_agent=sender.agent, sender_developer=sender.developer,
        recipient_agent=recipient.agent if recipient else "", body=body.body,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if recipient:
        await broadcast_event(recipient.id, "message.received", {"message_id": row.id})
    return row


@router.get("/tickets/{ticket_id}/handoffs")
async def ticket_handoffs(ticket_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Handoff).where(
        Handoff.ticket_id == ticket_id).order_by(Handoff.created_at, Handoff.id)
    )).scalars().all()
    linked = (await db.execute(select(AgentMessage).where(
        AgentMessage.handoff_id.in_([row.id for row in rows])
    ))).scalars().all() if rows else []
    messages = {message.handoff_id: message for message in linked}
    return [{"id": row.id, "ticket_id": row.ticket_id,
             "source_session_id": row.source_session_id,
             "recipient_session_id": row.recipient_session_id,
             "verdict": row.verdict, "blockers": json.loads(row.blockers),
             "next_steps": json.loads(row.next_steps),
             "artifacts": json.loads(row.artifacts),
             "message_id": messages[row.id].id if row.id in messages else None,
             "acknowledged_at": (messages[row.id].acknowledged_at
                                 if row.id in messages else None),
             "created_at": row.created_at} for row in rows]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
async def message_inbox(session_id: str, request: Request,
                        pending_only: bool = True,
                        limit: int = Query(20, ge=1, le=100),
                        db: AsyncSession = Depends(get_db)):
    await _actor(session_id, request, db)
    query = select(AgentMessage).where(AgentMessage.recipient_session_id == session_id)
    if pending_only:
        query = query.where(AgentMessage.acknowledged_at.is_(None))
    rows = (await db.execute(query.order_by(AgentMessage.created_at, AgentMessage.id)
                             .limit(limit))).scalars().all()
    return rows


@router.post("/messages/{message_id}/acknowledge", response_model=MessageResponse)
async def acknowledge_message(message_id: str, body: MessageAck, request: Request,
                              db: AsyncSession = Depends(get_db)):
    await _actor(body.recipient_session_id, request, db)
    row = await db.get(AgentMessage, message_id)
    if row is None or row.recipient_session_id != body.recipient_session_id:
        raise HTTPException(404, "Message not found in this session inbox")
    if row.acknowledged_at is None:
        row.acknowledged_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return row


@router.get("/messages/{message_id}", response_model=MessageResponse)
async def message_status(message_id: str, sender_session_id: str, request: Request,
                         db: AsyncSession = Depends(get_db)):
    await _actor(sender_session_id, request, db, require_active=False)
    row = await db.get(AgentMessage, message_id)
    if row is None or row.sender_session_id != sender_session_id:
        raise HTTPException(404, "Message not found in this session outbox")
    return row
