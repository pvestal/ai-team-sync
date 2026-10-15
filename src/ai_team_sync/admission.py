"""Work admission at the existing scope-claim doors (#3591).

WHY THIS EXISTS
---------------
A session claims files through start_session or extend_scope (POST /api/locks).
Those two doors already decide WHETHER a claim collides with a live lock. They
did not ask WHAT the work is, so a session could start implementing a
capability that an open ticket, a reviewed branch or a live owner already
covered, and nothing recorded that it had looked.

At the first governed claim of a session this module:
  1. classifies the work: continue (the session's ticket) | new | related |
     duplicate | exception -- the worker's own statement;
  2. records what existing work was visible at that moment (unfinished
     branches on the scope, advisory overlap candidates) next to that
     statement, as a worker-proposal decision row, so shadow mode measures
     real admission decisions instead of guessing;
  3. refuses, only in enforce mode and only for repositories the operator
     listed, the two cases that need no similarity judgment at all:
       - the worker classified the claim as a duplicate of another ticket;
       - a ticketless claim on governed files with no classification.

WHAT IT NEVER DOES
------------------
Block on similarity or on same-file overlap; select a ticket; write operator
rulings; act when ATS itself is down (the existing guards keep their
fail-closed behavior for governed canonical mutation).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.models import Decision, Session
from ai_team_sync.operator_config import OperatorConfigError, admission_mode_for
from ai_team_sync.schemas import AdmissionClaim

logger = logging.getLogger(__name__)

ADMISSION_TITLE = "work admission (#3591)"
SNAPSHOT_TIMEOUT_S = 4.0


@dataclass
class Verdict:
    classification: str
    mode: str
    hard_conflicts: list[dict] = field(default_factory=list)
    surfaced: dict = field(default_factory=dict)

    @property
    def refuse(self) -> bool:
        return self.mode == "enforce" and bool(self.hard_conflicts)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["would_refuse"] = bool(self.hard_conflicts)
        out["refused"] = self.refuse
        return out


def evaluate(claim: AdmissionClaim | None, *, ticket_id: int | None, governed: bool,
             mode: str) -> Verdict:
    """Pure: the verdict for one claim. No I/O, no similarity."""
    if claim is not None:
        classification = claim.kind
    else:
        classification = "continue" if ticket_id else "unclassified"
    verdict = Verdict(classification=classification, mode=mode)
    if claim is not None and claim.kind == "duplicate":
        target = claim.duplicate_of
        verdict.hard_conflicts.append({
            "code": "declared_duplicate",
            "message": (f"this claim is classified as a duplicate of #{target}: continue "
                        f"#{target} (start_session with ticket_id={target}) instead of "
                        f"opening parallel work" if target else
                        "this claim is classified as a duplicate but names no duplicate_of"),
            "duplicate_of": target})
    if governed and ticket_id is None and claim is None:
        verdict.hard_conflicts.append({
            "code": "unclassified_canonical_claim",
            "message": ("a governed scope claim with no ticket must say what the work is: "
                        "pass ticket_id, or admission kind new/related (with a reason) or "
                        "exception (naming the operator authorization)")})
    return verdict


def validate(claim: AdmissionClaim | None, ticket_id: int | None) -> str | None:
    """A malformed classification is a 422, in every mode."""
    if claim is None:
        return None
    if claim.kind == "continue" and ticket_id is None:
        return "admission kind 'continue' needs the session's ticket_id"
    if claim.kind in ("new", "exception") and not claim.reason.strip():
        return f"admission kind '{claim.kind}' needs a reason"
    if claim.kind == "related" and not claim.related_to:
        return "admission kind 'related' needs related_to ticket ids"
    if claim.kind == "duplicate" and claim.duplicate_of is None:
        return "admission kind 'duplicate' needs duplicate_of"
    return None


def mode_for(repo_root: str) -> str:
    try:
        return admission_mode_for(repo_root)
    except OperatorConfigError as exc:
        # A broken operator file must not silently become enforcement or
        # silently drop the record; shadow keeps recording and refuses nothing.
        logger.warning("admission policy unreadable, using shadow: %s", exc)
        return "shadow"


async def surfaced_work(repo_root: str, scope: list[str], ticket_id: int | None,
                        objective: str) -> dict:
    """What existing work was visible at claim time, for the record. Bounded:
    a slow git or Echo makes this 'unavailable', never a slow claim."""
    from ai_team_sync.active_work import unfinished_branches
    from ai_team_sync.briefs import fetch_task_overlaps

    async def branches():
        found = await asyncio.to_thread(
            unfinished_branches, repo_root, scope=scope,
            tickets=[ticket_id] if ticket_id else [])
        return [{"branch": b.branch, "tickets": b.tickets, "conflict": b.conflict,
                 "files_in_scope": b.files_in_scope[:5]} for b in found]

    async def overlaps():
        found, error = await asyncio.to_thread(
            fetch_task_overlaps, objective, exclude_ids=[ticket_id] if ticket_id else [],
            limit=6, timeout=SNAPSHOT_TIMEOUT_S - 0.5)
        if error:
            raise RuntimeError(error)
        return [{"id": c.get("id"), "status": c.get("status"), "score": c.get("score")}
                for c in found]

    out: dict = {}
    results = await asyncio.gather(
        asyncio.wait_for(branches(), SNAPSHOT_TIMEOUT_S),
        asyncio.wait_for(overlaps(), SNAPSHOT_TIMEOUT_S) if objective.strip()
        else asyncio.sleep(0, result=[]),
        return_exceptions=True)
    for name, result in zip(("branches", "overlaps"), results):
        if isinstance(result, BaseException):
            out[name] = []
            out[f"{name}_status"] = f"unavailable ({type(result).__name__}: {result})"[:200]
        else:
            out[name] = result
            out[f"{name}_status"] = "ok"
    return out


def is_governed(repo_root: str) -> bool:
    """Same predicate as the commit guard, on the SHARED root, so a worktree of
    a governed repository is governed. Unreadable config: not governed here --
    admission then refuses nothing; the existing guards keep their own
    fail-closed handling of that config."""
    import os

    from ai_team_sync.git_utils import resolve_repo_roots
    from ai_team_sync.hooks.commit_guard import governed

    if not repo_root:
        return False
    _worktree, shared = resolve_repo_roots(os.path.join(repo_root, ".ats-admission"))
    try:
        return governed(shared or repo_root)
    except OperatorConfigError:
        return False


async def admit(claim: AdmissionClaim | None, *, ticket_id: int | None, repo_root: str,
                scope: list[str], objective: str) -> Verdict | None:
    """Evaluate one claim at a claim door. None when admission is off.
    Raises 422 for a malformed classification and 409 only for an enforced
    hard conflict; everything else is recorded and admitted."""
    from fastapi import HTTPException

    error = validate(claim, ticket_id)
    if error:
        raise HTTPException(422, detail={"error": "admission_invalid", "message": error})
    mode = mode_for(repo_root)
    if mode == "off":
        return None
    verdict = evaluate(claim, ticket_id=ticket_id, governed=is_governed(repo_root), mode=mode)
    verdict.surfaced = await surfaced_work(repo_root, scope, ticket_id, objective)
    if verdict.refuse:
        raise HTTPException(409, detail={
            "error": "admission_refused",
            "message": "; ".join(c["message"] for c in verdict.hard_conflicts),
            "hard_conflicts": verdict.hard_conflicts,
            "surfaced": verdict.surfaced})
    return verdict


async def already_admitted(db: AsyncSession, session_id: str) -> bool:
    row = (await db.execute(
        select(Decision.id).where(Decision.session_id == session_id)
        .where(Decision.title == ADMISSION_TITLE).limit(1))).first()
    return row is not None


async def record(db: AsyncSession, session: Session, verdict: Verdict,
                 claim: AdmissionClaim | None, scope: list[str]) -> None:
    """One worker-proposal decision row per admitted session. Never an operator
    ruling: the brief renders it under WORKER PROPOSALS."""
    reasoning = {"verdict": verdict.as_dict(),
                 "claim": claim.model_dump() if claim else None}
    db.add(Decision(
        session_id=session.id, ticket_id=session.ticket_id, title=ADMISSION_TITLE,
        chosen=verdict.classification,
        reasoning=json.dumps(reasoning, default=str)[:20000],
        files=json.dumps(scope[:50])))
