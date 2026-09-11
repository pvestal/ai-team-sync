"""Delegation endpoints — bounded work handed between workers.

Every guard here protects one of two invariants: the parent keeps ownership,
and a mode narrows authority rather than decorating a record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.delegation import MODES, READ_ONLY, prohibitions_for
from ai_team_sync.models import Delegation, Session

router = APIRouter(prefix="/delegations", tags=["delegations"])


class DelegationCreate(BaseModel):
    parent_session_id: str
    delegated_worker: str
    mode: str = READ_ONLY
    parent_task: str = ""
    repo_root: str = ""
    scope: list[str] = Field(default_factory=list)
    objective: str = ""
    acceptance: str = ""
    extra_prohibitions: list[str] = Field(default_factory=list)
    lease_minutes: int = 60


class DelegationReturn(BaseModel):
    result_summary: str = ""
    evidence: dict = Field(default_factory=dict)


class DelegationClose(BaseModel):
    state: str = "closed"          # closed | rejected
    verdict: str = ""


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _as_dict(d: Delegation) -> dict:
    return {
        "id": d.id,
        # The invariant, spelled out in the field name.
        "parent_owner_session_id": d.parent_session_id,
        "parent_task": d.parent_task,
        "delegating_worker": d.delegating_worker,
        "delegated_worker": d.delegated_worker,
        "mode": d.mode,
        "repo_root": d.repo_root,
        "scope": json.loads(d.scope or "[]"),
        "objective": d.objective,
        "acceptance": d.acceptance,
        "prohibitions": json.loads(d.prohibitions or "[]"),
        "child_session_id": d.child_session_id,
        "lease_expires_at": _aware(d.lease_expires_at).isoformat(),
        "state": d.state,
        "result_summary": d.result_summary,
        "evidence": json.loads(d.evidence or "{}"),
        "verdict": d.verdict,
        "created_at": _aware(d.created_at).isoformat(),
        "closed_at": _aware(d.closed_at).isoformat() if d.closed_at else None,
    }


async def open_delegations_for(db: AsyncSession, parent_session_id: str) -> list[Delegation]:
    rows = await db.execute(
        select(Delegation).where(Delegation.parent_session_id == parent_session_id,
                                 Delegation.state.in_(("open", "returned"))))
    return list(rows.scalars().all())


async def _is_a_delegated_child(db: AsyncSession, session_id: str) -> bool:
    row = await db.execute(
        select(Delegation.id).where(Delegation.child_session_id == session_id))
    return row.scalar_one_or_none() is not None


@router.post("", status_code=201)
async def create_delegation(body: DelegationCreate, db: AsyncSession = Depends(get_db)):
    if body.mode not in MODES:
        raise HTTPException(422, detail={"error": "bad_mode",
                                         "message": f"mode must be one of {MODES}"})
    if not body.acceptance.strip():
        raise HTTPException(
            422,
            detail={"error": "missing_acceptance",
                    "message": ("acceptance criteria are required: without them the "
                                "parent cannot reconcile what comes back, and "
                                "'it worked' becomes the acceptance test")})

    parent = await db.get(Session, body.parent_session_id)
    if parent is None:
        raise HTTPException(404, detail={"error": "no_such_parent"})

    # delegation_depth = 1. A chain of workers 'collaborating' on one bug is a
    # chain in which nobody owns it.
    if await _is_a_delegated_child(db, parent.id):
        raise HTTPException(
            409,
            detail={"error": "recursive_delegation",
                    "message": ("this session is itself a delegated child; a child "
                                "may not delegate onward. Return to your parent.")})

    d = Delegation(
        parent_session_id=parent.id,
        parent_task=body.parent_task,
        delegating_worker=parent.agent,
        delegated_worker=body.delegated_worker,
        mode=body.mode,
        repo_root=body.repo_root or parent.repo_root,
        scope=json.dumps(body.scope),
        objective=body.objective,
        acceptance=body.acceptance,
        prohibitions=json.dumps(sorted(set(prohibitions_for(body.mode))
                                       | set(body.extra_prohibitions))),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=body.lease_minutes),
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return _as_dict(d)


@router.get("")
async def list_delegations(parent_session_id: str = "", state: str = "",
                           db: AsyncSession = Depends(get_db)):
    stmt = select(Delegation)
    if parent_session_id:
        stmt = stmt.where(Delegation.parent_session_id == parent_session_id)
    if state:
        stmt = stmt.where(Delegation.state == state)
    rows = await db.execute(stmt.order_by(Delegation.created_at.desc()).limit(100))
    return [_as_dict(d) for d in rows.scalars().all()]


@router.get("/{delegation_id}")
async def get_delegation(delegation_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Delegation, delegation_id)
    if d is None:
        raise HTTPException(404, detail={"error": "no_such_delegation"})
    return _as_dict(d)


@router.post("/{delegation_id}/return")
async def return_delegation(delegation_id: str, body: DelegationReturn,
                            db: AsyncSession = Depends(get_db)):
    """The child returns evidence. This deliberately does not touch the parent."""
    d = await db.get(Delegation, delegation_id)
    if d is None:
        raise HTTPException(404, detail={"error": "no_such_delegation"})
    if d.state != "open":
        raise HTTPException(409, detail={"error": "not_open", "state": d.state})
    if _aware(d.lease_expires_at) <= datetime.now(timezone.utc):
        d.state = "expired"
        await db.commit()
        raise HTTPException(
            409,
            detail={"error": "lease_expired",
                    "message": ("the lease ran out; the parent must re-delegate "
                                "rather than accept work of unknown age")})

    d.state = "returned"
    d.result_summary = body.result_summary
    d.evidence = json.dumps(body.evidence)
    await db.commit()
    await db.refresh(d)
    return _as_dict(d)


@router.post("/{delegation_id}/close")
async def close_delegation(delegation_id: str, body: DelegationClose,
                           db: AsyncSession = Depends(get_db)):
    """The PARENT reconciles. Closing a child never advances the parent's own
    work — that is a separate, explicit act."""
    d = await db.get(Delegation, delegation_id)
    if d is None:
        raise HTTPException(404, detail={"error": "no_such_delegation"})
    if body.state not in ("closed", "rejected"):
        raise HTTPException(422, detail={"error": "bad_state"})

    d.state = body.state
    d.verdict = body.verdict
    d.closed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(d)
    return _as_dict(d)
