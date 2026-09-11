"""Effective authority for a session — base worker, narrowed by delegation.

Three distinct answers, deliberately not collapsed into one:

  base       what this worker class may do anywhere
  mode       the delegation envelope it is currently working under, if any
  effective  the intersection, which is what actually applies

Reporting only `base` is what let a READ_ONLY child be told it could edit and
commit. Reporting only `effective` would hide WHY it cannot.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.delegation import effective_authority, prohibitions_for
from ai_team_sync.models import Delegation, Session
from ai_team_sync.workers import registry

router = APIRouter(prefix="/authority", tags=["authority"])


def _auth_dict(auth) -> dict:
    return {"edit": auth.edit, "commit": auth.commit, "task_close": auth.task_close}


async def authority_for_session(db: AsyncSession, session_id: str) -> dict:
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, detail={"error": "no_such_session"})

    worker = registry().resolve(session.agent)
    row = await db.execute(
        select(Delegation).where(Delegation.child_session_id == session_id))
    delegation = row.scalar_one_or_none()

    out = {
        "session_id": session.id,
        "agent": session.agent,
        "worker": worker.name,
        "capabilities": list(worker.capabilities),
        "base_authority": _auth_dict(worker.authority),
        "delegation": None,
        "effective_authority": _auth_dict(worker.authority),
        "prohibitions": [],
    }
    if delegation is not None:
        eff = effective_authority(worker, delegation.mode)
        out["delegation"] = {
            "delegation_id": delegation.id,
            "mode": delegation.mode,
            "state": delegation.state,
            "parent_owner_session_id": delegation.parent_session_id,
        }
        out["effective_authority"] = _auth_dict(eff)
        out["prohibitions"] = prohibitions_for(delegation.mode)
        out["narrowed"] = out["effective_authority"] != out["base_authority"]
    else:
        out["narrowed"] = False
    return out


@router.get("/{session_id}")
async def get_authority(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    return await authority_for_session(db, session_id)
