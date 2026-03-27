"""Override request endpoints for agent-to-agent coordination."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_team_sync.database import get_db
from ai_team_sync.models import OverrideRequest, Session
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.events import broadcast_event
from ai_team_sync.approval_policy import ApprovalPolicy
from ai_team_sync.schemas import (
    OverrideRequestCreate,
    OverrideRequestResponse,
    OverrideRequestRespond,
)

router = APIRouter(prefix="/override-requests", tags=["override-requests"])


def _override_to_response(req: OverrideRequest) -> OverrideRequestResponse:
    return OverrideRequestResponse(
        id=req.id,
        requester_session_id=req.requester_session_id,
        owner_session_id=req.owner_session_id,
        conflicting_pattern=req.conflicting_pattern,
        justification=req.justification,
        status=req.status,
        response_message=req.response_message,
        created_at=req.created_at,
        responded_at=req.responded_at,
        expires_at=req.expires_at,
        requester_developer=req.requester_session.developer if req.requester_session else None,
        owner_developer=req.owner_session.developer if req.owner_session else None,
    )


@router.post("", response_model=OverrideRequestResponse, status_code=201)
async def create_override_request(
    body: OverrideRequestCreate, db: AsyncSession = Depends(get_db)
):
    """Request permission to proceed despite a lock conflict."""
    # Verify requester session exists
    result = await db.execute(
        select(Session).where(Session.id == body.requester_session_id)
    )
    requester_session = result.scalar_one_or_none()
    if not requester_session:
        raise HTTPException(404, "Requester session not found")

    # Find owner session from conflicting pattern
    # (In practice, this would come from the conflict detection)
    # For now, we'll need to pass owner_session_id in the request
    # Let me add that to the schema...

    # Actually, let's find it by pattern matching
    from ai_team_sync.models import ScopeLock
    from fnmatch import fnmatch

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScopeLock, Session)
        .join(Session)
        .where(ScopeLock.expires_at > now)
        .where(Session.status.in_(["active", "paused"]))
    )
    active_locks = list(result.all())

    owner_session_id = None
    for lock, session in active_locks:
        if lock.pattern == body.conflicting_pattern:
            owner_session_id = session.id
            break

    if not owner_session_id:
        raise HTTPException(404, "No active lock found for that pattern")

    # Create the override request
    request = OverrideRequest(
        requester_session_id=body.requester_session_id,
        owner_session_id=owner_session_id,
        conflicting_pattern=body.conflicting_pattern,
        justification=body.justification,
    )
    db.add(request)
    await db.flush()  # Get ID before policy check

    # Check auto-approval policy
    policy = ApprovalPolicy()
    auto_decision = policy.should_auto_approve(request)

    if auto_decision is not None:
        # Auto-approve or auto-deny
        request.status = "approved" if auto_decision else "denied"
        request.response_message = policy.get_auto_response_message(auto_decision)
        request.responded_at = datetime.now(timezone.utc)

    await db.commit()

    # Reload with relationships
    result = await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.id == request.id)
        .options(
            selectinload(OverrideRequest.requester_session),
            selectinload(OverrideRequest.owner_session),
        )
    )
    request = result.scalar_one()

    # Notify lock owner via webhook
    await dispatch("override.requested", {
        "request_id": request.id,
        "requester": requester_session.developer,
        "requester_agent": requester_session.agent,
        "pattern": body.conflicting_pattern,
        "justification": body.justification,
        "owner_session_id": owner_session_id,
    })

    # Broadcast to WebSocket subscribers
    event_data = {
        "request_id": request.id,
        "requester": requester_session.developer,
        "requester_agent": requester_session.agent,
        "pattern": body.conflicting_pattern,
        "justification": body.justification,
        "auto_decided": auto_decision is not None,
        "status": request.status,
    }

    await broadcast_event(owner_session_id, "override.requested", event_data)

    # If auto-decided, also notify requester immediately
    if auto_decision is not None:
        await broadcast_event(body.requester_session_id, "override.responded", {
            "request_id": request.id,
            "approved": auto_decision,
            "response_message": request.response_message,
            "owner": "auto-policy",
        })

    return _override_to_response(request)


@router.get("", response_model=list[OverrideRequestResponse])
async def list_override_requests(
    session_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List override requests. Filter by session (as requester or owner) or status."""
    query = select(OverrideRequest).options(
        selectinload(OverrideRequest.requester_session),
        selectinload(OverrideRequest.owner_session),
    )

    if session_id:
        # Show requests where this session is either requester or owner
        query = query.where(
            (OverrideRequest.requester_session_id == session_id)
            | (OverrideRequest.owner_session_id == session_id)
        )

    if status:
        query = query.where(OverrideRequest.status == status)
    else:
        # Default: only show pending requests
        query = query.where(OverrideRequest.status == "pending")

    # Auto-expire old requests
    now = datetime.now(timezone.utc)
    await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.status == "pending")
        .where(OverrideRequest.expires_at < now)
    )
    expired = (await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.status == "pending")
        .where(OverrideRequest.expires_at < now)
    )).scalars().all()

    for req in expired:
        req.status = "expired"
    if expired:
        await db.commit()

    query = query.order_by(OverrideRequest.created_at.desc())
    result = await db.execute(query)
    return [_override_to_response(req) for req in result.scalars().all()]


@router.get("/{request_id}", response_model=OverrideRequestResponse)
async def get_override_request(request_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific override request."""
    result = await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.id == request_id)
        .options(
            selectinload(OverrideRequest.requester_session),
            selectinload(OverrideRequest.owner_session),
        )
    )
    request = result.scalar_one_or_none()
    if not request:
        raise HTTPException(404, "Override request not found")
    return _override_to_response(request)


@router.post("/{request_id}/respond", response_model=OverrideRequestResponse)
async def respond_to_override_request(
    request_id: str, body: OverrideRequestRespond, db: AsyncSession = Depends(get_db)
):
    """Approve or deny an override request (called by lock owner)."""
    result = await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.id == request_id)
        .options(
            selectinload(OverrideRequest.requester_session),
            selectinload(OverrideRequest.owner_session),
        )
    )
    request = result.scalar_one_or_none()
    if not request:
        raise HTTPException(404, "Override request not found")

    if request.status != "pending":
        raise HTTPException(400, f"Request already {request.status}")

    # Check if expired
    now = datetime.now(timezone.utc)
    # Ensure both datetimes are timezone-aware for comparison
    expires_at = request.expires_at
    if expires_at.tzinfo is None:
        from datetime import timezone as tz
        expires_at = expires_at.replace(tzinfo=tz.utc)
    if expires_at < now:
        request.status = "expired"
        await db.commit()
        raise HTTPException(410, "Request has expired")

    # Update request
    request.status = "approved" if body.approved else "denied"
    request.response_message = body.message
    request.responded_at = now

    await db.commit()
    await db.refresh(request)

    # Notify requester via webhook
    await dispatch("override.responded", {
        "request_id": request.id,
        "approved": body.approved,
        "response_message": body.message,
        "owner": request.owner_session.developer if request.owner_session else "unknown",
        "requester_session_id": request.requester_session_id,
    })

    # Broadcast to WebSocket subscribers
    await broadcast_event(request.requester_session_id, "override.responded", {
        "request_id": request.id,
        "approved": body.approved,
        "response_message": body.message,
        "owner": request.owner_session.developer if request.owner_session else "unknown",
    })

    return _override_to_response(request)
