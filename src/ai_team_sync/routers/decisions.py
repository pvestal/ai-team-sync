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
from ai_team_sync.message_lifecycle import append_delivery_event
from ai_team_sync import external_reviewer, peer_identity
from ai_team_sync.routers.locks import acquisition_guard, cross_account
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
        record_kind=d.record_kind or "decision",
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
    if external_reviewer.external_session(session):
        raise HTTPException(403, "External reviewer sessions cannot record decisions")
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

    # A decision is activity the reaper counts, so it is recorded only on a
    # live session and serialized with retirement (#2811): written while the
    # session is live (keeping it live), or refused because retirement won.
    async with acquisition_guard(db):
        await db.refresh(session, ["status"])
        if session.status not in ("active", "paused"):
            raise HTTPException(409, f"Session is {session.status}; decision not recorded. "
                                     "Start a new session.")
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
            message = AgentMessage(
                sender_session_id=session.id, recipient_session_id=recipient.id,
                original_recipient_session_id=recipient.id, addressing_mode="session",
                sender_agent=session.agent, sender_developer=session.developer,
                recipient_agent=recipient.agent, ticket_id=decision.ticket_id,
                kind="decision",
                body=(f"Decision {decision.id} — {decision.title}\n"
                      f"Chosen: {decision.chosen}\nReasoning: {decision.reasoning}"),
            )
            append_delivery_event(message, "assigned", recipient.id, "addressed_decision")
            db.add(message)
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
    repository: str | None = None,
    record_kind: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """`repo_root` matches a session's recorded root exactly. `repository`
    (#3591) matches every session of that repository, its linked worktrees
    included wherever they live; `record_kind` keeps one kind of record."""
    query = select(Decision).order_by(Decision.created_at.desc())
    if record_kind:
        query = query.where(Decision.record_kind == record_kind)
    if repository:
        return await _decisions_of_repository(db, query, repository, session_id=session_id,
                                              ticket_id=ticket_id, since=since,
                                              limit=limit or 200)
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


async def _decisions_of_repository(db, query, repository, *, session_id, ticket_id, since,
                                   limit: int) -> list[DecisionResponse]:
    from ai_team_sync.git_utils import repository_of

    want = repository_of(repository)
    query = query.add_columns(Session.repo_root).join(Session, Decision.session_id == Session.id)
    if session_id:
        query = query.where(Decision.session_id == session_id)
    if ticket_id is not None:
        query = query.where(Decision.ticket_id == ticket_id)
    if since is not None:
        query = query.where(Decision.created_at >= since)
    out: list[DecisionResponse] = []
    # Newest first, matched in Python (a worktree's repository is a path fact,
    # not a column), read in batches until the page is full or the table ends:
    # a short page always means "no more", never "stopped looking".
    offset = 0
    while len(out) < limit:
        batch = (await db.execute(query.offset(offset).limit(_SCAN_BATCH))).all()
        for decision, root in batch:
            if repository_of(root) == want:
                out.append(_decision_to_response(decision))
                if len(out) >= limit:
                    break
        if len(batch) < _SCAN_BATCH:
            break
        offset += _SCAN_BATCH
    return out


_SCAN_BATCH = 1000


@router.get("/{decision_id}", response_model=DecisionResponse)
async def get_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Decision).where(Decision.id == decision_id))
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(404, "Decision not found")
    return _decision_to_response(decision)
