"""Session CRUD endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from fnmatch import fnmatch

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_team_sync.database import get_db
from ai_team_sync.background_tasks import replace_lifecycle_marker
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.git_utils import uncommitted_for_scope
from ai_team_sync.notifications.dispatcher import dispatch
from ai_team_sync.schemas import SessionCreate, SessionResponse, SessionUpdate
from ai_team_sync.workers import registry
from ai_team_sync.delegation import effective_authority
from ai_team_sync.models import Delegation
from ai_team_sync.config import settings
from ai_team_sync import peer_identity
from ai_team_sync.scope_paths import UnsafePath, canonical_claim, canonical_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _session_liveness(s: Session) -> tuple[float | None, bool]:
    """Idle seconds since the session's most recent activity, and whether it is
    'stale' — silent past the heartbeat window (session_heartbeat_timeout_minutes,
    default 20m). Stale flags a suspected ghost in team_status WELL BEFORE the
    reaper auto-completes it (which is session_inactivity_hours, default 4h, for
    never-heartbeated clients), so its scope stops reading as a live blocker.

    Activity = newest of started_at, last_heartbeat, and its newest
    lock/decision/commit — the same signal the reaper uses
    (background_tasks.auto_complete_stale_sessions). Requires the locks/decisions/
    commits relationships to be loaded (list_sessions selectinloads them).
    """
    def _aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt

    times = [s.started_at, s.last_heartbeat]
    times += [x.created_at for x in (s.locks or [])]
    times += [x.created_at for x in (s.decisions or [])]
    times += [x.created_at for x in (s.commits or [])]
    aware = [_aware(t) for t in times if t]
    if not aware:
        return None, False
    idle = (datetime.now(timezone.utc) - max(aware)).total_seconds()
    is_stale = s.status == "active" and idle > settings.session_heartbeat_timeout_minutes * 60
    return idle, is_stale



def _uncommitted_in_scope(s: Session, cache: dict[str, list[str]] | None) -> list[str]:
    """ACTIVE session's uncommitted files that fall inside its scope.

    The active-only policy lives here rather than in the computation (#2554):
    reporting a live `git status` against a session completed hours ago would
    attribute whoever is dirty in that repo now to that session. What a
    completed session stranded is recorded at reap time instead — see
    background_tasks.auto_complete_stale_sessions — because it can only be
    known before the status flips.

    `cache` memoizes the git call per repo_root within one request — sessions
    frequently share a repo. Unanchored/legacy sessions ('' repo_root) return [].
    """
    if s.status != "active":
        return []
    scope = json.loads(s.scope) if s.scope else []
    return uncommitted_for_scope(getattr(s, "repo_root", "") or "", scope, cache)


def _session_to_response(s: Session, uncommitted_cache: dict[str, list[str]] | None = None) -> SessionResponse:
    idle_seconds, is_stale = _session_liveness(s)
    return SessionResponse(
        id=s.id,
        developer=s.developer,
        agent=s.agent,
        scope=json.loads(s.scope) if s.scope else [],
        description=s.description,
        status=s.status,
        branch=s.branch,
        repo_root=getattr(s, "repo_root", "") or "",
        started_at=s.started_at,
        completed_at=s.completed_at,
        last_heartbeat=s.last_heartbeat,
        summary=s.summary,
        lock_count=len(s.locks) if s.locks else 0,
        decision_count=len(s.decisions) if s.decisions else 0,
        commit_count=len(s.commits) if s.commits else 0,
        idle_seconds=idle_seconds,
        is_stale=is_stale,
        auto_completed=bool(getattr(s, "auto_completed", False)),
        uncommitted_in_scope=_uncommitted_in_scope(s, uncommitted_cache),
    )


async def _check_scope_conflicts(
    db: AsyncSession,
    new_patterns: list[str],
    current_developer: str,
    repo_root: str = "",
    exclude_session_id: str = "",
) -> list[dict]:
    """Check if new scope patterns conflict with existing active locks.

    `repo_root` anchors the check: locks held by sessions anchored to a
    DIFFERENT repo use patterns relative to that repo, so they cannot conflict
    with this session's patterns ('' on either side = legacy match-everywhere).
    `exclude_session_id` leaves out the requester's own locks: a session
    extending its own scope does not conflict with itself.
    """
    from ai_team_sync.routers.locks import _cross_repo, _get_active_locks

    # Same notion of a live lock as the board and the grant check (#2741).
    active_locks = await _get_active_locks(db)

    conflicts = []
    for new_pattern in new_patterns:
        for lock, owner, lock_repo_root in active_locks:
            if exclude_session_id and lock.session_id == exclude_session_id:
                continue
            if _cross_repo(repo_root, lock_repo_root):
                continue  # other repo's patterns can't collide with ours
            # Check if patterns overlap using bidirectional matching
            # Pattern A matches Pattern B, or Pattern B matches Pattern A
            if (fnmatch(new_pattern, lock.pattern) or
                fnmatch(lock.pattern, new_pattern) or
                new_pattern == lock.pattern):
                conflicts.append({
                    "new_pattern": new_pattern,
                    "existing_pattern": lock.pattern,
                    "existing_developer": owner.developer,
                    "lock_mode": lock.mode,
                    "session_id": lock.session_id,
                })

    return conflicts


def blocking_conflict(conflicts: list[dict], requested_mode: str) -> dict | None:
    """The conflict that refuses a new claim, or None when the overlap is shared.

    Refused when an overlapping lock is exclusive, or when the request itself is
    exclusive and anything overlaps. Session creation and POST /api/locks apply
    this one rule (#2756).
    """
    exclusive = [c for c in conflicts if c["lock_mode"] == "exclusive"]
    if exclusive:
        return exclusive[0]
    if requested_mode == "exclusive" and conflicts:
        return conflicts[0]
    return None


def scope_conflict_detail(what: str, conflict: dict, conflicts: list[dict]) -> dict:
    """The 409 body for a refused claim; `what` names the refused object."""
    mode_msg = (
        f"exclusive lock '{conflict['existing_pattern']}'"
        if conflict["lock_mode"] == "exclusive"
        else f"existing lock '{conflict['existing_pattern']}' (you requested exclusive mode)"
    )
    return {
        "error": "scope_conflict",
        "message": (
            f"Cannot create {what}: scope '{conflict['new_pattern']}' conflicts "
            f"with {mode_msg} held by {conflict['existing_developer']}"
        ),
        "conflicts": conflicts,
    }




async def _active_sessions_for_worker(db: AsyncSession, worker_name: str) -> int:
    """Active sessions governed by this worker class.

    Counted by RESOLVING each label, not by string match: 'claude-code:a1b2' and
    'claude-code:c3d4' are two sessions of one worker, and 'local:qwen3-30b' and
    'local:gpt-oss-20b' both draw on the same local budget.
    """
    rows = await db.execute(select(Session).where(Session.status == "active"))
    reg = registry()
    return sum(1 for s in rows.scalars().all()
               if reg.resolve_for_session(s)[0].name == worker_name)


def _authority_gate(body: SessionCreate, active_for_worker: int, worker=None,
                    binding_refusal: str | None = None) -> None:
    """Refuse a claim the worker has no authority to make. HTTPException or None.

    This is the half of coordination that cannot live in a client. The scope
    guard Claude Code runs is a PreToolUse hook; Codex has no hooks and a local
    worker has no client, so a client-side rule binds exactly one of the three.
    For unbound classes it is a guardrail, not access control — the label is
    unauthenticated. `worker` arrives already resolved against the connecting
    OS account for identity-bound classes, with `binding_refusal` saying why a
    bound class was not granted.
    """
    worker = worker or registry().resolve(body.agent)

    if body.scope and not worker.may_claim_scope:
        why = (f"{binding_refusal}, so it is treated as '{worker.name}' with edit authority 'none'"
               if binding_refusal else f"worker '{worker.name}' has edit authority 'none'")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "worker_authority",
                "message": (
                    f"{why}, so it cannot claim "
                    f"scope {body.scope}. Register unscoped and attach findings to the task "
                    f"instead — reading, triaging and proposing need no claim."
                ),
                "worker": worker.as_dict(),
            },
        )

    if worker.concurrency is not None and active_for_worker >= worker.concurrency:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "worker_concurrency",
                "message": (
                    f"worker '{worker.name}' already has {active_for_worker} active "
                    f"session(s) and is capped at {worker.concurrency}"
                ),
                "limit": worker.concurrency,
                "worker": worker.as_dict(),
            },
        )


def emit_session_completed(session: Session) -> None:
    """Tell Echo Brain a session finished. Fire and forget, by design.

    ATS owns current work state; it does not own a queue and gets no Redis
    client. It announces the completion over HTTP and stops caring. Every
    failure mode here — Echo Brain down, slow, refusing — must be invisible to
    the caller, because the alternative is a completed session whose locks are
    still held while an unrelated support service is unreachable. Memory
    ingestion is asynchronous support work; session completion is authoritative.
    """
    url = os.environ.get("ECHO_BRAIN_URL", "http://localhost:8309")
    if os.environ.get("ATS_EMIT_COMPLETION", "1") == "0":
        return
    payload = {"session_id": session.id, "agent": session.agent,
               "repo_root": session.repo_root or "", "summary": session.summary or ""}

    async def _send() -> None:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{url}/api/ats/session-completed", json=payload)
        except Exception as exc:  # noqa: BLE001 — never surfaces to the caller
            logger.info("session-completed event not delivered for %s: %s",
                        session.id, exc)

    try:
        asyncio.get_running_loop().create_task(_send())
    except Exception:  # noqa: BLE001 — no loop, no event; still never fatal
        pass


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreate, request: Request,
                         db: AsyncSession = Depends(get_db)):
    # Authority BEFORE conflicts: whether this worker may claim at all precedes
    # whether the claim collides with someone else's.
    #
    # Caller identity is established HERE and only here (#2741). An
    # identity-bound class is granted to this session only when the kernel says
    # the connection belongs to one of its accounts; anyone else naming it is
    # restricted. What was established is written to the row, so no later
    # request re-derives identity from the label.
    peer_uid = peer_identity.peer_uid_for_request(request)
    worker, bound_uid, binding_refusal = registry().resolve_at_create(body.agent, peer_uid)
    active_for_worker = (
        await _active_sessions_for_worker(db, worker.name)
        if worker.concurrency is not None else 0
    )
    _authority_gate(body, active_for_worker, worker, binding_refusal)

    # A bound session's claims are what a later grant is measured against, so
    # each must have exactly one meaning: an absolute root plus exact files or
    # 'dir/**' subtrees. Ambiguity is refused here, not interpreted later.
    bound = bound_uid is not None
    scope = list(body.scope)
    repo_root = body.repo_root
    if bound and scope:
        repo_root = canonical_root(body.repo_root)
        if not repo_root:
            raise HTTPException(422, detail={
                "error": "bound_claim_needs_repo_root",
                "message": "identity-bound claims are repo-relative; give an absolute repo_root"})
        try:
            scope = [canonical_claim(p) for p in body.scope]
        except UnsafePath as exc:
            raise HTTPException(422, detail={
                "error": "ambiguous_claim",
                "message": f"{exc}. Identity-bound claims name exact files or 'dir/**'."}) from exc

    # A delegated child is narrowed by its mode on top of its worker class.
    # READ_ONLY that only decorated the record would be a note attached to a
    # worker which can still edit six files.
    delegation = None
    if body.delegation_id:
        delegation = await db.get(Delegation, body.delegation_id)
        if delegation is None:
            raise HTTPException(404, detail={"error": "no_such_delegation"})
        auth = effective_authority(worker, delegation.mode)
        if body.scope and auth.edit == "none":
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "delegation_authority",
                    "message": (
                        f"delegation {delegation.id} is {delegation.mode}, so this "
                        f"child cannot claim scope {body.scope}. Register unscoped, "
                        f"investigate, and return evidence to the parent."
                    ),
                    "mode": delegation.mode,
                    "prohibitions": json.loads(delegation.prohibitions or "[]"),
                },
            )

    if delegation is not None:
        # One child, set once, while open, by the account that owns the parent
        # (#2741). Re-pointing a delegation at another session used to strip the
        # mode's narrowing off the child it had named — reproduced as a READ_ONLY
        # child gaining task_close.
        if delegation.child_session_id or delegation.state != "open":
            raise HTTPException(409, detail={
                "error": "delegation_child_taken",
                "message": (f"delegation {delegation.id} is {delegation.state} and "
                            f"{'already has' if delegation.child_session_id else 'has no'} "
                            f"a child; a delegation's child is set once, while it is open"),
            })
        from ai_team_sync.routers.locks import cross_account
        parent = await db.get(Session, delegation.parent_session_id)
        if parent is None or parent.status not in ("active", "paused") \
                or cross_account(peer_uid, parent):
            raise HTTPException(403, detail={
                "error": "not_the_parents_account",
                "message": (f"only the account that owns delegation {delegation.id}'s live "
                            f"parent session may open its child"),
            })

    # Check for scope conflicts BEFORE creating the session
    if body.auto_lock and scope:
        conflicts = await _check_scope_conflicts(
            db, scope, body.developer, repo_root=repo_root)

        if conflicts:
            new_lock_mode = getattr(body, 'lock_mode', settings.lock_default_mode)
            conflict = blocking_conflict(conflicts, new_lock_mode)
            if conflict is not None:
                raise HTTPException(
                    status_code=409,
                    detail=scope_conflict_detail("session", conflict, conflicts),
                )

            # Advisory conflicts: warn via notification but allow
            for conflict in conflicts:
                await dispatch("lock.conflict", {
                    "new_pattern": conflict["new_pattern"],
                    "existing_pattern": conflict["existing_pattern"],
                    "new_developer": body.developer,
                    "existing_developer": conflict["existing_developer"],
                })

    session = Session(
        developer=body.developer,
        agent=body.agent,
        scope=json.dumps(scope),
        description=body.description,
        branch=body.branch,
        repo_root=repo_root,
        creator_uid=peer_uid,
        bound_worker=worker.name if bound else "",
        bound_uid=bound_uid,
        task_id=body.task_id,
        delegation_id=delegation.id if delegation is not None else None,
    )
    db.add(session)
    await db.flush()  # Ensure session.id is populated
    if delegation is not None:
        delegation.child_session_id = session.id

    # Auto-create scope locks from scope patterns. Only a bound session's
    # creation-time claims bear authority; see ScopeLock.authority_bearing.
    if body.auto_lock and scope:
        lock_mode = getattr(body, 'lock_mode', settings.lock_default_mode)
        for pattern in scope:
            lock = ScopeLock(session_id=session.id, pattern=pattern, mode=lock_mode,
                             authority_bearing=bound)
            db.add(lock)

    await db.commit()

    # Reload with relationships
    result = await db.execute(
        select(Session)
        .where(Session.id == session.id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one()

    await dispatch("session.started", {
        "developer": session.developer,
        "agent": session.agent,
        "scope": body.scope,
        "description": session.description,
        "branch": session.branch,
        "session_id": session.id,
    })

    return _session_to_response(session)


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    status: str | None = None,
    developer: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Session).options(
        selectinload(Session.locks),
        selectinload(Session.decisions),
        selectinload(Session.commits),
    )
    if status:
        query = query.where(Session.status == status)
    if developer:
        query = query.where(Session.developer == developer)
    query = query.order_by(Session.started_at.desc())

    result = await db.execute(query)
    uncommitted_cache: dict[str, list[str]] = {}
    return [_session_to_response(s, uncommitted_cache) for s in result.scalars().all()]


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    return _session_to_response(session)


def _refuse_foreign_change(request: Request, session: Session, body: SessionUpdate) -> None:
    """A session is changed by the OS account that created it, whatever its
    status (#2741) — including reviving or annotating one the reaper completed.

    Completing someone's session releases their locks, including exclusive ones
    a mutation grant checks for conflicts, so this is not cosmetic: without it a
    bound worker could clear another session's exclusive claim and then be
    granted the files it protected. Another account's silent session is left to
    the in-process reaper. A session whose creator was never identified (legacy
    rows) follows cross_account's legacy rule: ordinary identified accounts may
    change it, unidentifiable callers and headless bound accounts may not.

    An identity-bound session is stricter: its authority ends with it (no
    reopening), its anchor is part of what its claims mean (not movable), and
    anything that raises what it may do comes from its bound account only.
    """
    from ai_team_sync.routers.locks import cross_account

    peer = peer_identity.peer_uid_for_request(request)
    if cross_account(peer, session, any_status=True):
        raise HTTPException(403, detail={
            "error": "session_not_yours",
            "message": (f"session {session.id} ({session.agent}) belongs to another OS account; "
                        f"it cannot be completed, revived or changed from here. Another "
                        f"account's silent session is left to the reaper."),
            "session_id": session.id})
    if not getattr(session, "bound_worker", ""):
        return
    if session.status == "completed" and body.status not in (None, "completed"):
        raise HTTPException(409, detail={
            "error": "bound_session_terminal",
            "message": "an identity-bound session's authority ended with it; start a new session"})
    if body.repo_root is not None and canonical_root(body.repo_root) != (session.repo_root or ""):
        raise HTTPException(409, detail={
            "error": "bound_session_anchor_fixed",
            "message": "an identity-bound session's repo_root is fixed at creation"})
    raises = ((body.status == "active" and session.status != "active")
              or body.scope is not None or body.description is not None)
    if raises and peer != session.bound_uid:
        raise HTTPException(403, detail={
            "error": "session_not_yours",
            "message": f"only the account bound to session {session.id} may change it"})


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(session_id: str, body: SessionUpdate, request: Request,
                         db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    _refuse_foreign_change(request, session, body)

    if body.status == "completed":
        # Ownership cannot be dropped while a child is still out. Completing
        # here would strand the child and leave the task owned by nobody —
        # the failure the delegation-is-not-handoff rule exists to prevent.
        from ai_team_sync.routers.delegations import open_delegations_for
        outstanding = await open_delegations_for(db, session.id)
        if outstanding:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "open_delegations",
                    "message": (
                        f"{len(outstanding)} child delegation(s) still open or awaiting "
                        f"reconciliation; close or reject them before releasing this "
                        f"session — you still own the task."
                    ),
                    "delegations": [d.id for d in outstanding],
                },
            )

    if body.status is not None:
        session.status = body.status
        if body.status == "completed":
            session.completed_at = datetime.now(timezone.utc)
            # Release all locks
            for lock in session.locks:
                await db.delete(lock)
    if body.summary is not None:
        session.summary = body.summary
    if body.scope is not None:
        session.scope = json.dumps(body.scope)
    if body.description is not None:
        session.description = body.description
    if body.repo_root is not None:
        # Anchor (or re-anchor) the session. Stored rstrip'd because
        # find_conflicts and the repo-anchoring comparisons are string equality
        # on this value — '/opt/anime-studio/' and '/opt/anime-studio' must not
        # read as two different repos.
        session.repo_root = body.repo_root.rstrip("/")

    await db.commit()
    await db.refresh(session)

    if body.status == "completed":
        # Support-staff intake, alongside the human-facing notification. Both
        # are after the commit, so neither can describe a state that did not land.
        emit_session_completed(session)
        await dispatch("session.completed", {
            "developer": session.developer,
            "agent": session.agent,
            "branch": session.branch,
            "summary": session.summary or "",
            "session_id": session.id,
        })

    return _session_to_response(session)


@router.post("/{session_id}/complete", response_model=SessionResponse)
async def complete_session_alias(
    session_id: str, request: Request, body: SessionUpdate | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Alias for PATCH {status:'completed'} (#2517 failure 2).

    Agents guess this RESTful-looking path, got {"detail":"Not Found"}, and
    concluded ATS was down — then proceeded uncoordinated. Delegates to
    update_session so completion semantics (lock release, completed_at,
    session.completed dispatch) stay single-sourced.
    """
    patch = SessionUpdate(status="completed",
                          summary=(body.summary if body else None))
    return await update_session(session_id, patch, request, db)


@router.post("/{session_id}/heartbeat", response_model=SessionResponse)
async def heartbeat_session(session_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Liveness ping: bump last_heartbeat to now. Cheap, idempotent, called often
    by a live client (e.g. a per-turn Stop hook). Gives the reaper a fast path to
    reclaim a dead session's locks instead of waiting the full inactivity window
    (see background_tasks.auto_complete_stale_sessions + Gap 1 doc)."""
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(selectinload(Session.locks), selectinload(Session.decisions), selectinload(Session.commits))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    # Only the owning account proves a session alive (#2741). A heartbeat from
    # anyone else was reproduced moving a never-heartbeating session from the 4h
    # reaper window onto the 20-minute one, so the reaper released its exclusive
    # lock and a bound worker was granted the file. Resurrection included.
    from ai_team_sync.routers.locks import cross_account
    if cross_account(peer_identity.peer_uid_for_request(request), session, any_status=True):
        raise HTTPException(403, detail={
            "error": "session_not_yours",
            "message": (f"session {session.id} belongs to another OS account; only its "
                        f"owner's heartbeat proves it alive"),
            "session_id": session.id})

    # A heartbeat for a COMPLETED session is the highest-signal event this server
    # can receive, and it used to be written to the corpse and forgotten.
    # Observed live 2026-08-10: bc62c5e9 completed 03:39:55, heartbeated 03:43:49;
    # 3436c282 heartbeated 2h19m after completion. Two different meanings:
    #
    #   auto_completed  -> the REAPER guessed, and this ping disproves it. The
    #                      process is alive, so bring the session back with its
    #                      locks rather than forcing a new id that severs
    #                      continuity with the work already claimed against it.
    #   operator-completed -> the operator said done. A late hook from a dying
    #                      process must not reopen it; refuse and do NOT stamp,
    #                      so a corpse never looks alive.
    if session.status == "completed":
        # An identity-bound session never comes back: its grants must not
        # outlive it, whoever completed it and whoever is pinging (#2741).
        if getattr(session, "bound_worker", ""):
            raise HTTPException(
                409,
                "Identity-bound session is completed; its authority ended with it. "
                "Start a new session.",
            )
        if not getattr(session, "auto_completed", False):
            raise HTTPException(
                409,
                "Session was completed by its owner; heartbeat refused. "
                "Start a new session rather than reopening finished work.",
            )
        note = "[resurrected: heartbeat proved the reap wrong]"
        session.status = "active"
        session.completed_at = None
        session.auto_completed = False
        session.summary = replace_lifecycle_marker(session.summary, note)
        logger.warning(
            "session %s resurrected: it was auto-completed as silent but is alive",
            session.id,
        )
        await dispatch("session.resurrected", {
            "session_id": session.id,
            "developer": session.developer,
            "agent": session.agent,
        })

    session.last_heartbeat = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return _session_to_response(session)
