"""Git status endpoints for session change tracking."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.git_utils import (
    files_match_patterns,
    get_repo_root,
    get_uncommitted_files,
)
from ai_team_sync.models import Session

router = APIRouter(prefix="/git", tags=["git"])


class SessionChangesResponse(BaseModel):
    """Uncommitted changes within a session's scope."""

    session_id: str
    scope_patterns: list[str]
    uncommitted_files: list[str]
    files_by_pattern: dict[str, list[str]]
    total_files: int


@router.get("/session/{session_id}/changes", response_model=SessionChangesResponse)
async def get_session_changes(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get uncommitted git changes that fall within this session's scope."""
    # Get session
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    scope_patterns = json.loads(session.scope) if session.scope else []
    if not scope_patterns:
        return SessionChangesResponse(
            session_id=session_id,
            scope_patterns=[],
            uncommitted_files=[],
            files_by_pattern={},
            total_files=0,
        )

    # Get uncommitted files from git
    repo_root = get_repo_root()
    uncommitted = get_uncommitted_files(repo_root)

    # Match files against session scope
    files_by_pattern = files_match_patterns(uncommitted, scope_patterns)

    # Flatten to get all matching files
    all_matching = list({f for files in files_by_pattern.values() for f in files})

    return SessionChangesResponse(
        session_id=session_id,
        scope_patterns=scope_patterns,
        uncommitted_files=all_matching,
        files_by_pattern=files_by_pattern,
        total_files=len(all_matching),
    )


class PreCommitCheckRequest(BaseModel):
    """Request to check if staged files conflict with active locks."""

    staged_files: list[str] | None = None  # If None, auto-detect from git


class PreCommitCheckResponse(BaseModel):
    """Result of pre-commit lock check."""

    can_proceed: bool
    warnings: list[str]
    blocking_locks: list[dict]
    advisory_locks: list[dict]


@router.post("/pre-commit-check", response_model=PreCommitCheckResponse)
async def pre_commit_check(
    body: PreCommitCheckRequest, db: AsyncSession = Depends(get_db)
):
    """
    Check if staged files conflict with active locks.

    Used by pre-commit hook to warn/block commits.
    """
    from ai_team_sync.git_utils import get_staged_files
    from ai_team_sync.models import ScopeLock
    from ai_team_sync.routers.locks import _get_active_locks

    # Get staged files
    if body.staged_files is None:
        repo_root = get_repo_root()
        staged_files = get_staged_files(repo_root)
    else:
        staged_files = body.staged_files

    if not staged_files:
        return PreCommitCheckResponse(
            can_proceed=True, warnings=[], blocking_locks=[], advisory_locks=[]
        )

    # Get active locks
    active_locks = await _get_active_locks(db)

    blocking_locks = []
    advisory_locks = []
    warnings = []

    for file in staged_files:
        for lock, developer in active_locks:
            from fnmatch import fnmatch

            if fnmatch(file, lock.pattern):
                lock_info = {
                    "file": file,
                    "pattern": lock.pattern,
                    "developer": developer,
                    "mode": lock.mode,
                }

                if lock.mode == "exclusive":
                    blocking_locks.append(lock_info)
                    warnings.append(
                        f"BLOCKED: {file} matches exclusive lock '{lock.pattern}' held by {developer}"
                    )
                else:
                    advisory_locks.append(lock_info)
                    warnings.append(
                        f"WARNING: {file} matches advisory lock '{lock.pattern}' held by {developer}"
                    )

    can_proceed = len(blocking_locks) == 0

    return PreCommitCheckResponse(
        can_proceed=can_proceed,
        warnings=warnings,
        blocking_locks=blocking_locks,
        advisory_locks=advisory_locks,
    )
