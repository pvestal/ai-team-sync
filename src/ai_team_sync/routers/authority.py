"""Effective authority for a session — base worker, narrowed by delegation.

Three distinct answers, deliberately not collapsed into one:

  base       what this worker class may do anywhere
  mode       the delegation envelope it is currently working under, if any
  effective  the intersection, which is what actually applies

Reporting only `base` is what let a READ_ONLY child be told it could edit and
commit. Reporting only `effective` would hide WHY it cannot.

A fourth answer is separate again (#2741): whether ATS will GRANT an
authoritative mutation. Base and effective describe a CLASS; for an unbound
class the label that selected it is unproven, so that authority is coordination
policy. POST /{session_id}/authorize is the only place a grant is made, and it
grants only to an identity-bound session asked from its bound OS account
(operator ruling #2622: computation may continue without ATS, authoritative
mutation may not). Every answer is recorded in authority_checks.
"""

from __future__ import annotations

import json
import os
import posixpath
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync import peer_identity
from ai_team_sync.database import get_db
from ai_team_sync.delegation import effective_authority, intersect_authority, prohibitions_for
from ai_team_sync.models import AuthorityCheck, Delegation, ScopeLock, Session
from ai_team_sync.scope_paths import (
    UnsafePath,
    canonical_pattern,
    canonical_relpath,
    canonical_root,
    claim_covers,
    literal_prefix,
    may_overlap,
    resolved_relpath,
)
from ai_team_sync.workers import Authority, registry

router = APIRouter(prefix="/authority", tags=["authority"])

# The whole vocabulary. Nothing else is ever granted; service control is not an
# ATS grant today and is refused as an unknown action.
MUTATION_ACTIONS = ("commit", "land", "task_close")
_MAX_PATHS = 500
_DENIED = Authority()


def _auth_dict(auth: Authority) -> dict:
    return {"edit": auth.edit, "commit": auth.commit, "land": auth.land,
            "task_close": auth.task_close}


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


async def _delegations_of(db: AsyncSession, session_id: str) -> list[Delegation]:
    rows = await db.execute(select(Delegation).where(Delegation.child_session_id == session_id))
    return list(rows.scalars().all())


async def _delegation_for(db: AsyncSession, session: Session) -> tuple[Delegation | None, str | None]:
    """The delegation governing `session`, or why it cannot be determined.

    It must be the one recorded on the session at creation AND still name the
    session as its child. Any disagreement is a refusal reason, never a reason
    to treat the session as undelegated.
    """
    rows = await _delegations_of(db, session.id)
    recorded = getattr(session, "delegation_id", None)
    if not rows and not recorded:
        return None, None
    if recorded and len(rows) == 1 and rows[0].id == recorded:
        return rows[0], None
    return None, ("delegation linkage for this session is inconsistent (recorded "
                  f"{recorded!r}, pointed at by {[d.id for d in rows]}); no grant")


async def authority_for_session(db: AsyncSession, session_id: str) -> dict:
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, detail={"error": "no_such_session"})

    reg = registry()
    worker, bound, note = reg.resolve_for_session(session)
    delegation_row, link_note = await _delegation_for(db, session)
    legacy_child = False
    if link_note and not getattr(session, "delegation_id", None):
        rows = await _delegations_of(db, session_id)
        if len(rows) == 1:  # a child created before #2741: report its narrowing
            delegation_row, link_note, legacy_child = rows[0], None, True

    out = {
        "session_id": session.id,
        "agent": session.agent,
        "worker": worker.name,
        "capabilities": list(worker.capabilities),
        "base_authority": _auth_dict(worker.authority),
        "delegation": None,
        "effective_authority": _auth_dict(worker.authority),
        "prohibitions": [],
        "identity_bound": bound,
        "bound_worker": session.bound_worker or None,
        "bound_uid": session.bound_uid,
        "task_id": session.task_id,
        # Whether POST /authorize can grant anything at all for this session.
        # False for every unbound session: its authority above is advisory.
        "grantable": bound and session.status == "active" and reg.config_error is None,
        "binding_note": note or (None if bound else (
            "advisory: this worker class is not identity-bound, so ATS grants this "
            "session no authoritative mutation")),
    }
    if link_note:
        out["effective_authority"] = _auth_dict(_DENIED)
        out["grantable"] = False
        out["narrowed"] = True
        out["binding_note"] = link_note
    elif delegation_row is not None:
        delegation = delegation_row
        if legacy_child:
            out["grantable"] = False
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


class AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: StrictStr
    repo_root: StrictStr = ""
    # Repo-relative paths the mutation touches. commit and land are granted for
    # named files inside this session's own identity-bound claim, never for a
    # repository.
    paths: list[StrictStr] = Field(default_factory=list, max_length=_MAX_PATHS)
    task_id: StrictInt | None = None
    # For conditional close authority: the acceptance evidence the caller will
    # close against. ATS requires that it exists; its content is the caller's
    # evidence gate to adjudicate. Only the keys are recorded.
    evidence: dict[str, Any] = Field(default_factory=dict)


def _resolve_literal(absolute_pattern: str) -> str:
    """The pattern with its glob-free leading directories resolved through
    symlinks, so a lock written as 'lnk/a.py' also names 'src/a.py'."""
    parts = absolute_pattern.lstrip("/").split("/")
    literal, _ = literal_prefix(parts)
    if not literal:
        return absolute_pattern
    real = os.path.realpath("/" + "/".join(literal)).rstrip("/")
    rest = parts[len(literal):]
    return "/".join([real] + rest) if rest else (real or "/")


def _checkouts_of(root: str) -> tuple[str, list[str]] | None:
    """(this checkout's root, every checkout of its repository), by reading the
    repository's own worktree registry. No subprocess; None if not a git repo."""
    from ai_team_sync.git_utils import resolve_repo_roots

    # resolve_repo_roots walks up from a FILE's directory, so name a path inside
    # the root; handing it the root itself would start at the root's parent.
    worktree, shared = resolve_repo_roots(os.path.join(root, ".ats-root"))
    if not worktree or not shared:
        return None
    found = {worktree, shared}
    registry_dir = os.path.join(shared, ".git", "worktrees")
    try:
        names = os.listdir(registry_dir)
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(registry_dir, name, "gitdir"), encoding="utf-8") as fh:
                gitfile = fh.read().strip()
        except OSError:
            continue
        if gitfile:
            found.add(os.path.dirname(os.path.normpath(gitfile)))
    return worktree, sorted(found)


def _path_forms(root: str, paths: list[str], *, every_checkout: bool) -> dict[str, set[str]]:
    """Absolute forms of each path to compare against locks: as spelled, under
    the real root, fully resolved — and, for `land`, the same repo-relative file
    in EVERY checkout of the repository, because landing ships into all of them.
    """
    real_root = os.path.realpath(root)
    checkouts = _checkouts_of(root) if every_checkout else None
    out: dict[str, set[str]] = {}
    for p in paths:
        forms = {f"{root}/{p}", f"{real_root}/{p}", os.path.realpath(os.path.join(root, p))}
        if checkouts:
            worktree, every = checkouts
            rel = os.path.relpath(os.path.join(root, p), worktree)
            if rel != ".." and not rel.startswith("../"):
                for checkout in every:
                    forms |= {f"{checkout}/{rel}", f"{os.path.realpath(checkout)}/{rel}"}
        out[p] = {form.lstrip("/") for form in forms}
    return out


def _lock_hits(pattern: str, lock_root: str, root: str, forms: dict[str, set[str]]) -> list[str]:
    """Which of `paths` another session's lock may cover. Uncertain is covered.

    Compared in ABSOLUTE space, with the lock's own anchor applied: a lock held
    by a session anchored at a subdirectory, a parent directory, or a symlinked
    spelling of this repository is the same file claim, and a lock anchored to
    an unrelated tree simply does not overlap. An unanchored lock is read
    against this root (legacy match-everywhere). Both sides are also compared
    after resolving symlinks.
    """
    raw = (pattern or "").strip()
    if not raw:
        return []
    base = canonical_root(lock_root) or root
    pat = canonical_pattern(raw, base)
    if pat.startswith("/"):
        absolute = pat
    elif pat == "":
        absolute = base
    else:
        absolute = "/" + posixpath.normpath(f"{base}/{pat}").lstrip("/")
    candidates = {absolute.lstrip("/"), _resolve_literal(absolute).lstrip("/")}
    return [p for p, path_forms in forms.items()
            if any(may_overlap(form, cand) for form in path_forms for cand in candidates)]


def _meaningful(evidence: dict) -> bool:
    """Evidence has at least one non-empty value. Its content is not judged here."""
    return any(v not in (None, "", [], {}) for v in evidence.values())


@router.post("/{session_id}/authorize")
async def authorize(session_id: str, request: Request,
                    db: AsyncSession = Depends(get_db)) -> dict:
    """Allow or refuse one authoritative mutation. Refusal is the default.

    Every check can only add a reason, and the mutation is allowed only when
    none did. Checks keep running after the first reason so the record says
    everything that was wrong. A malformed request or an unknown session is a
    refusal with an audit row, never an unrecorded 4xx.

    Identity comes from what the session recorded at creation plus the kernel's
    answer for THIS request — never from a label in this request.
    """
    reg = registry()
    peer_uid = peer_identity.peer_uid_for_request(request)
    reasons: list[str] = []

    raw: Any = None
    body: AuthorizeRequest | None = None
    try:
        raw = await request.json()
        body = AuthorizeRequest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — malformed is a refusal
        first = (str(exc).splitlines() or [type(exc).__name__])[0]
        reasons.append(f"malformed authorize request: {first[:200]}")
    if body is not None:
        action = body.action
    elif isinstance(raw, dict) and isinstance(raw.get("action"), str):
        action = raw["action"]
    else:
        action = ""

    if reg.config_error:
        reasons.append("the worker registry configuration was rejected, so no "
                       "identity-bound class is in force")

    session = await db.get(Session, session_id)
    worker_name = ""
    bound = False
    auth = _DENIED
    delegation: Delegation | None = None
    if session is None:
        reasons.append("no such session")
    else:
        worker, bound, note = reg.resolve_for_session(session)
        worker_name = worker.name
        auth = worker.authority
        if session.status != "active":
            reasons.append(f"session is {session.status}: authority ends with its session "
                           f"and cannot be reused")
        if note:
            reasons.append(note)
        elif not bound:
            reasons.append(f"session is not identity-bound: worker '{worker.name}' holds "
                           f"advisory coordination authority only; ATS grants mutations "
                           f"only to a class bound to an OS account")
        if peer_uid is None:
            reasons.append("the OS account behind this request could not be established "
                           "(forwarding header, non-loopback peer, or no single kernel "
                           "socket owner)")
        elif bound and peer_uid != session.bound_uid:
            reasons.append(f"session is bound to uid {session.bound_uid}; this request "
                           f"comes from uid {peer_uid}")

        delegation, link_note = await _delegation_for(db, session)
        if link_note:
            reasons.append(link_note)
            auth = _DENIED
        elif delegation is not None:
            auth = effective_authority(worker, delegation.mode)
            if delegation.state != "open":
                reasons.append(f"delegation {delegation.id} is {delegation.state}")
            if _aware(delegation.lease_expires_at) <= datetime.now(timezone.utc):
                reasons.append(f"delegation {delegation.id} lease has expired")
            parent = await db.get(Session, delegation.parent_session_id)
            parent_worker, parent_bound, _ = (reg.resolve_for_session(parent)
                                              if parent is not None else (None, False, None))
            if (parent is None or parent.status != "active" or not parent_bound
                    or parent.bound_uid != session.bound_uid):
                reasons.append("a delegated child holds a grant only while its parent is an "
                               "active identity-bound session on the same OS account")
                auth = _DENIED
            else:
                auth = intersect_authority(auth, parent_worker.authority)

    now = datetime.now(timezone.utc)
    root = ""
    paths: list[str] = []
    if body is not None:
        label = worker_name or "restricted"
        if action not in MUTATION_ACTIONS:
            reasons.append(f"unknown action {action!r}; ATS authorizes only "
                           f"{list(MUTATION_ACTIONS)}")
        elif action in ("commit", "land"):
            if getattr(auth, action) is not True:
                reasons.append(f"worker '{label}' has no {action} authority here")
            if auth.edit != "claimed_scope":
                reasons.append(f"worker '{label}' has edit authority '{auth.edit}' here")
            root = canonical_root(body.repo_root)
            if not root:
                reasons.append("repo_root must be an absolute path")
            elif session is not None and root != canonical_root(session.repo_root):
                reasons.append(f"session is anchored to {session.repo_root!r}, "
                               f"not {body.repo_root!r}")
            if not body.paths:
                reasons.append("paths are required: authority is granted for named files")
            for raw_path in body.paths:
                try:
                    paths.append(canonical_relpath(raw_path))
                except UnsafePath as exc:
                    reasons.append(str(exc)[:300])
            paths = list(dict.fromkeys(paths))

            resolved: dict[str, str | None] = (
                {p: resolved_relpath(root, p) for p in paths} if root else {})
            for p, real in resolved.items():
                if real is None:
                    reasons.append(f"{p!r} resolves outside the repository")

            if session is not None and paths:
                own = (await db.execute(
                    select(ScopeLock).where(ScopeLock.session_id == session.id))).scalars().all()
                claims = [lock.pattern for lock in own
                          if lock.authority_bearing and _aware(lock.expires_at) > now]

                def covered(q: str) -> bool:
                    return any(claim_covers(c, q) for c in claims)

                # Both the path as spelled and where it really lands must be claimed.
                unclaimed = [p for p in paths
                             if not covered(p) or (resolved.get(p) and not covered(resolved[p]))]
                if unclaimed:
                    reasons.append(f"paths outside this session's identity-bound live claim: "
                                   f"{unclaimed}")

            if root and paths:
                # Every exclusive lock of a LIVE session, expired or not: a lock
                # that outlived its TTL while its owner is still working is
                # still that owner's claim as far as a grant is concerned.
                live = await db.execute(
                    select(ScopeLock, Session.developer, Session.repo_root)
                    .join(Session).where(Session.status.in_(("active", "paused"))))
                forms = _path_forms(root, paths, every_checkout=(action == "land"))
                for lock, developer, lock_root in live.all():
                    if session is not None and lock.session_id == session.id:
                        continue
                    if lock.mode != "exclusive":
                        continue
                    held = _lock_hits(lock.pattern, lock_root, root, forms)
                    if held:
                        from ai_team_sync.routers.override_requests import approved_override_for_lock
                        if await approved_override_for_lock(db, session_id, lock):
                            continue
                        reasons.append(f"{held} may be under exclusive lock {lock.pattern!r} "
                                       f"held by {developer} (session {lock.session_id})")
        else:  # task_close
            if auth.task_close not in ("yes", "conditional"):
                reasons.append(f"worker '{label}' has no task_close authority here")
            if body.task_id is None:
                reasons.append("task_id is required")
            elif session is not None and session.task_id != body.task_id:
                reasons.append(
                    f"session was opened for task {session.task_id}, not {body.task_id}"
                    if session.task_id is not None else
                    "session was opened without a task; close authority is scoped to the "
                    "task a session declares at creation")
            if auth.task_close == "conditional" and not _meaningful(body.evidence):
                reasons.append("conditional close authority requires acceptance evidence")

    allowed = not reasons
    recorded_paths = paths or [str(p)[:1024] for p in (body.paths if body else [])]
    check = AuthorityCheck(
        session_id=session_id[:36],
        agent=(session.agent if session else "")[:100],
        worker=worker_name[:100],
        bound_worker=(session.bound_worker or "") if session else "",
        bound_uid=session.bound_uid if session else None,
        peer_uid=peer_uid,
        action=action[:40],
        repo_root=(root or (body.repo_root if body else ""))[:1024],
        paths=json.dumps(recorded_paths),
        task_id=body.task_id if body else None,
        delegation_id=delegation.id if delegation else None,
        evidence_keys=json.dumps(sorted(str(k)[:100] for k in body.evidence)[:50] if body else []),
        allowed=allowed,
        reasons=json.dumps(reasons),
    )
    db.add(check)
    await db.commit()
    return {
        "allowed": allowed,
        "action": action,
        "session_id": session_id,
        "agent": session.agent if session else None,
        "worker": worker_name or None,
        "identity_bound": bound,
        "bound_uid": session.bound_uid if session else None,
        "peer_uid": peer_uid,
        "effective_authority": _auth_dict(auth),
        "repo_root": root or None,
        # Canonical form. A caller that was granted must act on THESE paths.
        "paths": paths,
        "task_id": body.task_id if body else None,
        "reasons": reasons,
        "check_id": check.id,
        "checked_at": _aware(check.created_at).isoformat() if check.created_at else None,
    }
