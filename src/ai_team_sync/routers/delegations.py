"""Delegation endpoints — bounded work handed between workers.

Every guard here protects one of two invariants: the parent keeps ownership,
and a mode narrows authority rather than decorating a record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync import peer_identity
from ai_team_sync.database import get_db
from ai_team_sync.delegation import MODES, READ_ONLY, prohibitions_for
from ai_team_sync.launch_spec import RoutingFailure, validate_resolution
from ai_team_sync.models import Delegation, Session
from ai_team_sync.workers import registry

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
    # The absolute executable the caller resolved before spawning, and the
    # launch contract it used. Empty is accepted only for callers that do not
    # spawn anything (tests, tooling that records an already-finished exchange).
    resolved_binary: str = ""
    launch_spec_version: str = ""


class DelegationReturn(BaseModel):
    result_summary: str = ""
    evidence: dict = Field(default_factory=dict)
    # The CHILD submits its result. Exact id, not a worker name.
    actor_session_id: str = ""


class DelegationClose(BaseModel):
    state: str = "closed"          # closed | rejected
    verdict: str = ""
    # Only the PARENT OWNER reconciles. Accepting a result is the owner's
    # judgement of evidence against acceptance criteria; a child that could
    # close its own delegation would be marking its own homework.
    actor_session_id: str = ""


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
        # Same value, named for what it actually is. A reader deciding whether
        # to trust "Codex reviewed this" must compare these two, never read the
        # worker name alone.
        "requested_worker": d.delegated_worker,
        "resolved_binary": d.resolved_binary or None,
        "launch_spec_version": d.launch_spec_version or None,
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


def _refuse_other_account(request: Request, session: Session | None, role: str) -> None:
    """Delegation state is changed only by the OS account that owns the session
    it belongs to (#2741): a foreign delegation pins another account's session
    open, and a foreign close/return revokes or advances another account's
    child. Session ids are public, so naming the right one proves nothing.

    Whatever the session's status: a delegation keeps belonging to the account
    that created its sessions after the parent is reaped (it can revive on its
    next heartbeat) or the child dies. Exempting non-live sessions let another
    account return a childless delegation under a reaped parent, or plant a
    forged result through a dead child (adversarial review round 5). For rows
    that never recorded a creator, cross_account's legacy rule applies instead.

    A session row that no longer exists establishes no owner, so nobody may
    change the delegation through it (round 6: a missing row skipped the check).
    """
    from ai_team_sync.routers.locks import cross_account

    if session is None:
        raise HTTPException(409, detail={
            "error": "session_missing",
            "message": (f"this delegation's {role} session no longer exists, so its "
                        f"owner cannot be established; the delegation is left as it is")})
    if cross_account(peer_identity.peer_uid_for_request(request), session, any_status=True):
        raise HTTPException(403, detail={
            "error": "session_not_yours",
            "message": f"the {role} session {session.id} belongs to another OS account",
            "session_id": session.id})


@router.post("", status_code=201)
async def create_delegation(body: DelegationCreate, request: Request,
                            db: AsyncSession = Depends(get_db)):
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

    # Record-only callers may omit a binary for REGISTERED classes. This must
    # never be an alternate admission route for an unregistered worker. Refuse
    # before database access, row creation, child registration or spawn.
    if registry().registered(body.delegated_worker) is None:
        raise HTTPException(409, detail={
            "error": "unregistered_worker",
            "requested_worker": body.delegated_worker,
            "message": "unregistered worker: refusing delegation; no launch provenance created",
        })

    # Re-derive the routing rule server-side rather than trusting the caller's
    # pairing. A record may claim worker X only if the binary that was resolved
    # is X's. requested=codex + resolved=claude is a ROUTING FAILURE and must
    # never be stored as a satisfied Codex delegation (observed 2026-09-12).
    if body.resolved_binary:
        try:
            validate_resolution(body.delegated_worker, body.resolved_binary)
        except RoutingFailure as exc:
            raise HTTPException(
                409,
                detail={"error": "routing_failure",
                        "requested_worker": body.delegated_worker,
                        "resolved_binary": body.resolved_binary,
                        "message": str(exc)}) from exc

    parent = await db.get(Session, body.parent_session_id)
    if parent is None:
        raise HTTPException(404, detail={"error": "no_such_parent"})
    _refuse_other_account(request, parent, "parent")

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
        resolved_binary=body.resolved_binary,
        launch_spec_version=body.launch_spec_version,
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
async def return_delegation(delegation_id: str, body: DelegationReturn, request: Request,
                            db: AsyncSession = Depends(get_db)):
    """The child returns evidence. This deliberately does not touch the parent."""
    d = await db.get(Delegation, delegation_id)
    if d is None:
        raise HTTPException(404, detail={"error": "no_such_delegation"})
    if d.state != "open":
        raise HTTPException(409, detail={"error": "not_open", "state": d.state})
    # Returned from the child's account; with no child yet, from the parent's.
    # Checked whatever either session's status is (see _refuse_other_account).
    # Skipping the check when there was no child let another account mark a
    # delegation returned and lock out the real child (review round 4).
    if d.child_session_id:
        _refuse_other_account(request, await db.get(Session, d.child_session_id), "child")
    else:
        _refuse_other_account(request, await db.get(Session, d.parent_session_id), "parent")
    if d.child_session_id and body.actor_session_id \
            and body.actor_session_id != d.child_session_id:
        raise HTTPException(
            403,
            detail={"error": "not_the_child",
                    "message": (f"only the delegated child ({d.child_session_id}) "
                                f"submits this result"),
                    "child_session_id": d.child_session_id})
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
async def close_delegation(delegation_id: str, body: DelegationClose, request: Request,
                           db: AsyncSession = Depends(get_db)):
    """The PARENT reconciles. Closing a child never advances the parent's own
    work — that is a separate, explicit act."""
    d = await db.get(Delegation, delegation_id)
    if d is None:
        raise HTTPException(404, detail={"error": "no_such_delegation"})
    if body.state not in ("closed", "rejected"):
        raise HTTPException(422, detail={"error": "bad_state"})
    _refuse_other_account(request, await db.get(Session, d.parent_session_id), "parent")
    if not body.actor_session_id:
        raise HTTPException(
            403,
            detail={"error": "no_actor",
                    "message": ("reconciling is an ownership act; identify the "
                                "session doing it (actor_session_id)")})
    if body.actor_session_id != d.parent_session_id:
        raise HTTPException(
            403,
            detail={"error": "not_the_owner",
                    "message": (f"delegation {d.id} is owned by session "
                                f"{d.parent_session_id}; {body.actor_session_id} "
                                f"cannot reconcile it"),
                    "parent_owner_session_id": d.parent_session_id})

    d.state = body.state
    d.verdict = body.verdict
    d.closed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(d)
    return _as_dict(d)
