"""Task-claim context packet."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.briefs import build_brief
from ai_team_sync.database import get_db

router = APIRouter(prefix="/brief", tags=["brief"])


class BriefRequest(BaseModel):
    objective: str
    repo_root: str = ""
    scope: list[str] = Field(default_factory=list)
    recall: bool = True          # consult Echo Brain; False keeps it ATS-only
    limit: int = 8


@router.post("")
async def post_brief(body: BriefRequest, db: AsyncSession = Depends(get_db)) -> dict:
    return await build_brief(db, objective=body.objective, repo_root=body.repo_root,
                             scope=body.scope, recall=body.recall, limit=body.limit)
