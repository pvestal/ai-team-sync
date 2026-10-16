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
from typing import Any

from pydantic import ValidationError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.models import Decision, Session
from ai_team_sync.operator_config import OperatorConfigError, admission_mode_for
from ai_team_sync.schemas import AdmissionClaim

logger = logging.getLogger(__name__)

ADMISSION_TITLE = "work admission (#3591)"  # display only
ADMISSION_RECORD_KIND = "work_admission"  # identity: Decision.record_kind, server-set
SNAPSHOT_TIMEOUT_S = 4.0


@dataclass
class Verdict:
    classification: str
    mode: str
    hard_conflicts: list[dict] = field(default_factory=list)
    surfaced: dict = field(default_factory=dict)
    # Worker dispositions next to what admission observed (claims vs facts).
    review: dict = field(default_factory=dict)
    # The worker's classification as parsed, and why it was malformed if it was.
    claim: dict | None = None
    invalid: str = ""

    @property
    def refuse(self) -> bool:
        return self.mode == "enforce" and bool(self.hard_conflicts)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["would_refuse"] = bool(self.hard_conflicts) or bool(self.invalid)
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


def cross_check(claim: AdmissionClaim | None, *, ticket_id: int | None,
                surfaced: dict) -> dict:
    """Compare the worker's dispositions with what admission itself observed.

    A disposition is a claim, never proof. Contradictions are recorded as
    OBSERVATIONS for review ("contested"); none of them refuses the claim,
    because e.g. a 'separate' change can legitimately touch the same file.
    """
    overlaps = {f"#{c['id']}": c for c in surfaced.get("overlaps") or [] if c.get("id")}
    branches = {f"branch:{b['branch']}": b for b in surfaced.get("branches") or []}
    by_ticket: dict[str, list[dict]] = {}
    for b in branches.values():
        for t in b.get("tickets") or []:
            by_ticket.setdefault(f"#{t}", []).append(b)
    seen = set(overlaps) | set(branches) | set(by_ticket)
    given = {d.ref: d for d in (claim.dispositions if claim else [])}
    contested, unverifiable = [], []
    for ref, d in given.items():
        if ref not in seen:
            unverifiable.append(ref)
        if d.disposition == "continuation" and ref.startswith("#") \
                and int(ref[1:]) != (ticket_id or 0):
            contested.append({"ref": ref, "claimed": d.disposition, "observed": (
                f"this session works #{ticket_id}" if ticket_id else "this session names no ticket")})
        if d.disposition == "separate":
            touching = [b for b in (by_ticket.get(ref, []) + ([branches[ref]] if ref in branches else []))
                        if b.get("files_in_scope")]
            for b in touching:
                contested.append({"ref": ref, "claimed": "separate", "observed": (
                    f"branch {b['branch']} changes files in this claim: "
                    + ", ".join(b["files_in_scope"][:4]))})
        status = (overlaps.get(ref) or {}).get("status")
        if d.disposition == "superseded" and status in ("pending", "in_progress", "blocked"):
            contested.append({"ref": ref, "claimed": "superseded",
                              "observed": f"{ref} is still {status}; only its owner or the "
                                          "operator closes it"})
        if d.disposition == "duplicate" and status in ("completed", "skipped"):
            contested.append({"ref": ref, "claimed": "duplicate",
                              "observed": f"{ref} is {status}"})
    return {"dispositions": [d.model_dump() for d in given.values()],
            "contested": contested, "unverifiable": unverifiable,
            "undispositioned": sorted(r for r in set(overlaps) | set(branches) if r not in given)}


def validate(claim: AdmissionClaim | None, ticket_id: int | None) -> str | None:
    """Why a classification is incomplete, or None. Refused only in enforce."""
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


def parse_claim(raw: Any, ticket_id: int | None) -> tuple[AdmissionClaim | None, str]:
    """(claim, error): the worker's classification parsed from the raw request
    field. A shape the schema rejects yields no claim; an incomplete one keeps
    the claim and names what is missing."""
    if raw is None:
        return None, ""
    try:
        claim = AdmissionClaim.model_validate(raw)
    except ValidationError as exc:
        return None, "malformed admission classification: " + "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'admission'}: {e['msg']}"
            for e in exc.errors()[:3])
    return claim, validate(claim, ticket_id) or ""


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


async def admit(raw: Any, *, ticket_id: int | None, repo_root: str,
                scope: list[str], objective: str) -> Verdict | None:
    """Evaluate one claim at a claim door. None when admission is off.
    Only enforce refuses: 422 for a malformed classification, 409 for a hard
    conflict. Shadow records a malformed classification and admits."""
    from fastapi import HTTPException

    mode = mode_for(repo_root)
    if mode == "off":
        return None
    claim, error = parse_claim(raw, ticket_id)
    if error and mode == "enforce":
        raise HTTPException(422, detail={"error": "admission_invalid", "message": error})
    verdict = evaluate(claim, ticket_id=ticket_id, governed=is_governed(repo_root), mode=mode)
    verdict.claim = claim.model_dump() if claim is not None else None
    if error:
        verdict.invalid = error
        verdict.claim = verdict.claim or {"unparsed": str(raw)[:2000]}
    verdict.surfaced = await surfaced_work(repo_root, scope, ticket_id, objective)
    verdict.review = cross_check(claim, ticket_id=ticket_id, surfaced=verdict.surfaced)
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
        .where(Decision.record_kind == ADMISSION_RECORD_KIND).limit(1))).first()
    return row is not None


async def record(db: AsyncSession, session: Session, verdict: Verdict,
                 scope: list[str]) -> None:
    """One worker-proposal decision row per admitted session. Never an operator
    ruling: the brief renders it under WORKER PROPOSALS."""
    reasoning = {"verdict": verdict.as_dict(), "claim": verdict.claim}
    db.add(Decision(
        session_id=session.id, ticket_id=session.ticket_id, title=ADMISSION_TITLE,
        record_kind=ADMISSION_RECORD_KIND,
        chosen=verdict.classification,
        reasoning=json.dumps(reasoning, default=str)[:20000],
        files=json.dumps(scope[:50])))
