"""Task-claim context packet."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.briefs import TaskContextUnavailable, build_brief
from ai_team_sync.caller_session import resolve_caller_session
from ai_team_sync.database import get_db
from ai_team_sync.models import Session

router = APIRouter(prefix="/brief", tags=["brief"])


class BriefRequest(BaseModel):
    objective: str
    repo_root: str = ""
    scope: list[str] = Field(default_factory=list)
    recall: bool = True          # consult Echo Brain; False keeps it ATS-only
    limit: int = 8
    task_id: int | None = Field(default=None, gt=0)
    render_task_context: bool = True
    # False is the project/repository status path. It deliberately avoids
    # guessing one ticket from broad project wording while still returning ATS
    # locks, decisions, prior work and repo-scoped handoffs.
    resolve_task: bool = True
    # The session this brief is for, so its own locks are not reported back to
    # it as blockers (#2757). A claim, validated against the caller's account.
    session_id: str = ""


@router.post("")
async def post_brief(body: BriefRequest, request: Request,
                     db: AsyncSession = Depends(get_db)) -> dict:
    # Same resolver as pre-commit-check: one identity rule for both readers.
    caller = await resolve_caller_session(db, request=request, session_id=body.session_id)
    try:
        packet = await build_brief(
            db, objective=body.objective, repo_root=body.repo_root,
            scope=body.scope, recall=body.recall, limit=body.limit,
            caller_session_id=caller.session_id,
            caller_identity_unresolved=caller.unresolved,
            task_id=body.task_id,
            render_task_context=body.render_task_context,
            resolve_task=body.resolve_task)
        packet["session_linkage"] = {"status": "not_applicable", "task_id": None}
        resolved_task_id = packet.get("task_id")
        if resolved_task_id is not None and caller.session_id is not None:
            session = (await db.execute(
                select(Session).where(Session.id == caller.session_id)
            )).scalar_one_or_none()
            if session is None:
                packet["session_linkage"] = {
                    "status": "session_missing", "task_id": resolved_task_id}
            elif session.ticket_id == resolved_task_id:
                packet["session_linkage"] = {
                    "status": "already_bound", "task_id": resolved_task_id}
            elif session.ticket_id is not None:
                raise HTTPException(status_code=409, detail={
                    "error": "task_linkage_conflict",
                    "message": (f"session {session.id} is already linked to ticket "
                                f"#{session.ticket_id}; it cannot be silently rebound to "
                                f"#{resolved_task_id}"),
                    "session_id": session.id,
                    "existing_ticket_id": session.ticket_id,
                    "resolved_ticket_id": resolved_task_id,
                })
            elif session.status != "active":
                packet["session_linkage"] = {
                    "status": "session_not_active", "task_id": resolved_task_id}
            else:
                # First task resolution may add handoff lineage, but never task
                # close authority.  The exact session capability is required;
                # same-account read identity alone is deliberately insufficient.
                from ai_team_sync.routers.sessions import (bind_ticket_lineage,
                                                            _require_session_capability)
                try:
                    _require_session_capability(request, session)
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                    packet["session_linkage"] = {
                        "status": "authorization_required", "task_id": resolved_task_id}
                else:
                    await bind_ticket_lineage(db, session, resolved_task_id)
                    await db.commit()
                    packet["session_linkage"] = {
                        "status": "bound", "task_id": resolved_task_id}
        return packet
    except TaskContextUnavailable as exc:
        raise HTTPException(status_code=409, detail={
            "error": "task_context_unavailable",
            "task_id": exc.task_id,
            "message": str(exc),
        }) from exc
