"""Observed per-file actions from instrumented agent clients."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync import peer_identity
from ai_team_sync.database import get_db
from ai_team_sync.models import FileActivity, Session
from ai_team_sync.routers.locks import cross_account

router = APIRouter(prefix="/file-activities", tags=["file-activities"])


class FileActivityCreate(BaseModel):
    session_id: str
    action: Literal["read", "edit"]
    path: str = Field(min_length=1, max_length=1024)
    repo_root: str = ""


class FileActivityResponse(FileActivityCreate):
    id: str
    agent: str
    developer: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("", response_model=FileActivityResponse, status_code=201)
async def record_file_activity(body: FileActivityCreate, request: Request,
                               db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, body.session_id)
    if (session is None or session.status != "active"
            or cross_account(peer_identity.peer_uid_for_request(request), session)):
        raise HTTPException(403, "Active session owned by caller is required")
    token = request.headers.get("X-ATS-Approval-Token", "")
    expected = session.approval_token_hash or ""
    if (not token or not expected or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected)):
        raise HTTPException(403, "Session capability is required to report file activity")
    row = FileActivity(**body.model_dump(), agent=session.agent,
                       developer=session.developer)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("", response_model=list[FileActivityResponse])
async def list_file_activities(session_id: str = "", limit: int = Query(100, ge=1, le=500),
                               db: AsyncSession = Depends(get_db)):
    stmt = select(FileActivity).order_by(FileActivity.created_at.desc()).limit(limit)
    if session_id:
        stmt = stmt.where(FileActivity.session_id == session_id)
    rows = (await db.execute(stmt)).scalars().all()
    return rows
