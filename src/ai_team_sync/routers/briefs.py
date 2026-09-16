"""Task-claim context packet."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.briefs import build_brief
from ai_team_sync.caller_session import resolve_caller_session
from ai_team_sync.database import get_db

router = APIRouter(prefix="/brief", tags=["brief"])


class BriefRequest(BaseModel):
    objective: str
    repo_root: str = ""
    scope: list[str] = Field(default_factory=list)
    recall: bool = True          # consult Echo Brain; False keeps it ATS-only
    limit: int = 8
    # The session this brief is for, so its own locks are not reported back to
    # it as blockers (#2757). A claim, validated against the caller's account.
    session_id: str = ""


@router.post("")
async def post_brief(body: BriefRequest, request: Request,
                     db: AsyncSession = Depends(get_db)) -> dict:
    # Same resolver as pre-commit-check: one identity rule for both readers.
    caller = await resolve_caller_session(db, request=request, session_id=body.session_id)
    return await build_brief(db, objective=body.objective, repo_root=body.repo_root,
                             scope=body.scope, recall=body.recall, limit=body.limit,
                             caller_session_id=caller.session_id,
                             caller_identity_unresolved=caller.unresolved)
