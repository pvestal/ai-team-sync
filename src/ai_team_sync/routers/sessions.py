"""Session CRUD endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from ai_team_sync.scope_paths import (UnsafePath, canonical_claim, canonical_pattern,
                                      canonical_root)

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


def _parse_unresolved(raw: str) -> list[tuple[str, str]]:
    """The stored (pattern, reason) pairs of CURRENTLY UNHELD lanes.

    Tolerant by design — this is read on every session response, so garbage
    yields nothing rather than raising. Accepts the bare list-of-strings shape
    as well, so a row written before reasons were stored still reads.
    """
    raw = raw or ""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[tuple[str, str]] = []
    for item in parsed:
        if isinstance(item, str):
            out.append((item, ""))
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
            reason = item[1] if len(item) > 1 and isinstance(item[1], str) else ""
            out.append((item[0], reason))
    return out


def _merge_unresolved(existing: list[tuple[str, str]],
                      new: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Union, newest reason wins, original order preserved.

    ADDITIVE ON PURPOSE (#2760). `locks_not_restored` is CURRENT AUTHORITY
    STATE, not the outcome of the latest lifecycle event: once ATS knows a
    session does not hold a lane, that stays true until a LIVE lock proves
    otherwise. Replacing the field per resurrection meant an ordinary second
    cycle — reaped again holding nothing, so the reaper journals an empty list,
    so the heartbeat resolves `nothing_journalled` with no refusals — wrote []
    over a loss that was still real, and the lane silently became authority
    again. A cycle that adjudicated nothing must change nothing.
    """
    merged = dict(existing)
    merged.update(dict(new))
    order = [p for p, _ in existing] + [p for p, _ in new if p not in dict(existing)]
    seen, out = set(), []
    for pattern in order:
        if pattern not in seen:
            seen.add(pattern)
            out.append((pattern, merged[pattern]))
    return out


def _live_lock_patterns(s: Session) -> set[str]:
    """Patterns this session holds under a lock that is LIVE RIGHT NOW.

    Same liveness rule the board and every conflict check use
    (routers.locks._get_active_locks): unexpired, or exclusive and held by a
    live owner. An EXPIRED ADVISORY row must not count — it is invisible to
    GET /api/locks and blocks nobody, so treating it as proof of authority let
    a dead row clear a live loss and hand the scope fallback back to a session
    holding nothing.
    """
    from ai_team_sync.routers.locks import live_exclusive_owner

    def _aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt

    now = datetime.now(timezone.utc)
    held = set()
    for lk in (s.locks or []):
        expires = _aware(getattr(lk, "expires_at", None))
        if (expires and expires > now) or (lk.mode == "exclusive" and live_exclusive_owner(s)):
            held.add(canonical_pattern(lk.pattern))
    return held


def _unrestored_state(s: Session) -> tuple[list[str], list[str]]:
    """(lanes, reasons) the session is known NOT to hold, parallel index for index.

    The subtraction is done on READ so that re-taking a lane stays an ordinary
    lock creation: nothing has to remember to clear a second field, and a missed
    write cannot leave the board claiming a lane is lost while its owner holds
    it. The locks table stays authoritative; this is durable negative knowledge
    that stops a historical `scope` from being mistaken for present authority.
    """
    # COMPARED CANONICALLY, because a lock and a loss are two spellings of the
    # same question. Raw string equality made a live 'src//**' fail to suppress
    # a 'src/**' loss although both canonicalise to the same coverage: the guard
    # granted (it consults live locks first) while the board went on telling the
    # owner the lane was unheld and had to be re-taken. Canonical equality is a
    # strict WIDENING of string equality — equal strings always share a
    # canonical form — so nothing that used to be suppressed stops being.
    #
    # ROOT-INDEPENDENT ON PURPOSE, and this is a correctness requirement rather
    # than a simplification. Passing the SESSION's repo_root here relativised an
    # absolute lock pattern, so a live '/repo/a.py' merged with a lost 'a.py'
    # and suppressed it — while the guard, which places patterns against the
    # FILE's root, does not relativise that lock when it runs with an empty root
    # and so grants nothing from it. The refusal vanished and `scope` alone let
    # the edit through: a REPORTING change that silently moved enforcement.
    # Canonicalising both sides against no root keeps this answer independent of
    # whichever root a reader happens to hold, so an absolute pattern and a
    # relative one never merge here.
    #
    # It deliberately does NOT reason about coverage between DIFFERENT globs:
    # 'src/*.py' and 'src/a*' stay distinct here, as they do everywhere else in
    # ATS (#2806 owns that question, and #2807 owns the broader-vs-narrower one).
    held = _live_lock_patterns(s)
    pairs = [(p, r) for p, r in _parse_unresolved(getattr(s, "locks_not_restored", ""))
             if canonical_pattern(p) not in held]
    return [p for p, _ in pairs], [r for _, r in pairs]


def _validated_scope(patterns: list[str]) -> list[str]:
    """Scope patterns put through the ORDINARY LOCK CONTRACT, or refused.

    THE ONE DOOR (#2760). A scope entry is authority-bearing twice over: it
    becomes ScopeLock rows, and hooks.pre_tool_use_lockcheck reads a scope
    pattern as a claim in its own right. So every supported write of one runs
    LockCreate's validator rather than restating its rules — which is why this
    is a shared function and not a second copy in each endpoint. Two doors that
    normalise differently is exactly what let 'src/** ' be stored verbatim by
    one and returned as the broader 'src/**' by the other.

    Raises HTTPException(400) so callers can validate BEFORE mutating anything.
    """
    from pydantic import ValidationError

    from ai_team_sync.schemas import LockCreate

    out = []
    for pattern in patterns:
        try:
            out.append(LockCreate(session_id="unassigned", pattern=pattern).pattern)
        except ValidationError as exc:
            raise HTTPException(400, detail={
                "error": "invalid_scope_pattern",
                "pattern": pattern,
                "message": (
                    f"{_first_error(exc)} Session scope creates locks and is read as a "
                    f"claim, so it must be path globs — put prose in `description`."),
            }) from exc
    return out


def _session_to_response(s: Session, uncommitted_cache: dict[str, list[str]] | None = None,
                         restoration: "RestorationOutcome | None" = None) -> SessionResponse:
    idle_seconds, is_stale = _session_liveness(s)
    return SessionResponse(
        restoration_outcome=restoration.outcome if restoration else OUTCOME_NOT_ATTEMPTED,
        restoration_reason=restoration.reason if restoration else "",
        restoration_detail=restoration.detail if restoration else "",
        locks_restored=list(restoration.restored) if restoration else [],
        # ALWAYS the durable state, never the event — on the resurrecting request
        # too. The caller that discards this body (the Stop hook, the MCP) must
        # not be the only thing that ever knew, and a later GET must not disagree
        # with the heartbeat that produced it. resurrect_session has already
        # merged this cycle's refusals in by the time any response is built, so
        # this is the same answer, minus whatever a live lock has since proven.
        locks_not_restored=_unrestored_state(s)[0],
        not_restored_reasons=_unrestored_state(s)[1],
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


# Restoration outcome STATES. Every one of these used to render to a caller as
# an empty refusal string, so "this request was not a revival", "it was, and the
# reaper had banked nothing" and "it was, and it was refused" were the same
# answer (#2760).
OUTCOME_NOT_ATTEMPTED = "not_attempted"
OUTCOME_RESTORED = "restored"
OUTCOME_NOTHING_JOURNALLED = "nothing_journalled"
OUTCOME_REFUSED = "refused"
OUTCOME_CONCURRENT = "concurrent"

# Refusal reasons. STRUCTURAL and pattern-free, so a reason can go into the
# lifecycle marker without any glob ever reaching narrative text.
REFUSAL_OWNER_COMPLETED = "owner_completed"
REFUSAL_IDENTITY_BOUND = "identity_bound"
REFUSAL_UNIDENTIFIED_OWNER = "unidentified_owner"
REFUSAL_ANCHOR_MOVED = "anchor_moved"
REFUSAL_INVALID_JOURNAL = "invalid_journal"

# Why a single lane did not come back, when the journal itself was fine.
NOT_RESTORED_NEWER_HOLDER = "newer_holder"
NOT_RESTORED_EXPIRED = "expired"

# One in-process claim per session, so two heartbeats for the SAME session never
# reach the database write lock together (#2760 F2).
#
# THE COMPARE-AND-SET ALONE IS NOT ENOUGH ON THE DEPLOYED DATABASE. ATS runs
# SQLite in journal_mode=delete (whole-file locking, no WAL). The winner's
# transaction holds the write lock from its UPDATE through to COMMIT, so a second
# connection issuing the same UPDATE does not get a clean "0 rows matched" — it
# BLOCKS, then raises `database is locked` after the driver's busy timeout, which
# surfaces as HTTP 500 on the hottest endpoint in the system and stalls every
# other ATS write for the same interval. Measured: 5.03s, then OperationalError.
#
# So the loser is turned away BEFORE it opens a write: it finds the claim held
# and returns OUTCOME_CONCURRENT without touching the database. The CAS stays as
# the correctness backstop — it is what makes "at most one grant" true even for a
# caller that somehow bypasses this lock — while this is what makes contention
# CHEAP. The scope of the guarantee is one process, which is what
# deploy/ats-server.service runs; a second server process against the same file
# would fall back to the CAS and to SQLite's lock, and that is a database-mode
# question, not one this seam can answer (see #2764).
_RESTORATION_CLAIMS: dict[str, asyncio.Lock] = {}


def journal_names_locks(raw: str | None) -> bool:
    """Does this journal actually carry lock authority worth protecting?

    NOT the same question as "is the column non-empty". The reaper writes an
    envelope for EVERY session it completes, so a session that held no locks is
    journalled as `{"repo_root": "...", "locks": []}` — a perfectly non-empty
    string. Keying the PATCH refusal on truthiness therefore made every reaped
    session permanently heartbeat-only, breaking `ats session pause` and the
    pause_session / resume_session MCP tools, which answered 200 at HEAD, and
    telling the operator the session "still holds an unspent lock-restoration
    journal" when it held nothing at all.

    A journal that cannot be parsed counts as carrying authority. That is the
    same fail-closed direction restoration itself takes for malformed input: it
    refuses the whole journal rather than treating unreadable as empty, and the
    PATCH guard must not be the one place where garbage reads as "nothing here".
    """
    if not raw:
        return False
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        return True
    if not isinstance(envelope, dict):
        return True
    entries = envelope.get("locks")
    if not isinstance(entries, list):
        return True
    return len(entries) > 0


def restoration_claim(session_id: str) -> asyncio.Lock:
    """The claim serialising restoration for one session.

    OWNED BY WHOEVER OWNS THE TRANSACTION, which is `heartbeat_session`. An
    earlier version acquired and released it inside `restore_reaped_locks`, and
    that was measured wrong: the restore had already issued its UPDATE and its
    INSERT, so the SQLite write lock was held from there until the caller's
    COMMIT — but the claim was released the moment the function returned. A
    contender arriving in that window found the claim free, issued its own
    compare-and-set, blocked on the write lock for the full driver busy timeout
    (5.01s measured) and died with `database is locked`, stalling every other
    ATS write with it. The protected region has to span the transaction, not
    the evaluation.

    Entries are never removed. Pruning them meant reading `Lock._waiters`, a
    CPython-private attribute, and popping the Lock object opened the same hole
    from the other side: a contender's `setdefault` would mint a fresh unlocked
    Lock and walk straight in. One small object per session that has reached
    restoration is the cheaper mistake.
    """
    return _RESTORATION_CLAIMS.setdefault(session_id, asyncio.Lock())

@dataclass
class RestorationOutcome:
    """What restoration did, in a form nothing has to parse out of prose."""

    outcome: str = OUTCOME_NOT_ATTEMPTED
    restored: list[str] = field(default_factory=list)
    not_restored: list[str] = field(default_factory=list)
    # PARALLEL to `not_restored`, index for index. A dict keyed by pattern
    # collapsed duplicates: a journal naming one lane twice with two different
    # outcomes produced two entries and one reason, and no caller could say
    # which reason belonged to which refusal.
    not_restored_reasons: list[str] = field(default_factory=list)
    reason: str = ""
    detail: str = ""

    @property
    def lost_the_claim(self) -> bool:
        """Another request holds the claim. THIS request restored nothing and
        must not narrate the one that did."""
        return self.outcome == OUTCOME_CONCURRENT


def _journalled_patterns(raw: str) -> list[str]:
    """The lane names a journal carries, read as tolerantly as possible.

    Used ONLY to record what a refusal leaves unheld (#2760). It must never
    raise and never guess: an unreadable journal yields [], because inventing a
    lane name would be worse than admitting we cannot name it. Nothing here
    grants anything — the grant path validates every entry through LockCreate
    separately.
    """
    try:
        envelope = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return []
    if not isinstance(envelope, dict):
        return []
    entries = envelope.get("locks")
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if isinstance(entry, dict):
            pattern = entry.get("pattern")
            if isinstance(pattern, str) and pattern.strip():
                out.append(pattern)
    return out


def _refused(reason: str, detail: str, lanes: "list[str] | tuple[str, ...]" = ()) -> RestorationOutcome:
    """A refusal, carrying the lanes it leaves unheld where they are knowable.

    A REFUSAL IS NOT AN ABSENCE OF INFORMATION. The reaper already deleted these
    rows; refusing to give them back is precisely the moment ATS learns the
    session does not hold them. Reporting no lanes here is what let a refused
    resurrection read as an ordinary active session whose `scope` still named
    every lane (#2760). When the journal cannot be parsed the lanes genuinely
    are unknown, and `lanes` is empty — but the caller MERGES rather than
    replaces, so unknown never erases a loss recorded earlier.
    """
    lanes = [p for p in lanes if isinstance(p, str)]
    return RestorationOutcome(outcome=OUTCOME_REFUSED, reason=reason, detail=detail,
                              not_restored=list(lanes),
                              not_restored_reasons=[reason] * len(lanes))


async def restore_reaped_locks(
    db: AsyncSession, session: Session, *, was_auto_completed: bool,
) -> RestorationOutcome:
    """Re-take the lane the reaper took (#2760).

    RE-ACQUISITION, NOT REVIVAL. Every journalled pattern goes through the same
    overlap rule that refuses any other claim (`_check_scope_conflicts` +
    `blocking_conflict`, #2756), so a holder that arrived while this session was
    dead always wins and is never overwritten, stolen or duplicated.

    WHAT THE JOURNAL IS, EXACTLY. It is what the REAPER wrote on its way past,
    and it is never reconstructed from `session.scope`: scope is a declaration,
    a lock is a grant, and synthesising one from the other would hand a
    resurrected session authority it never had. That is a statement about this
    function's INPUT, not a guarantee about the column. `sessions.reaped_locks`
    is written by `auto_complete_stale_sessions` and by nothing else — no
    schema, no request body and no MCP tool exposes it — so the only way to put
    a pattern in it is direct database write access, and anyone holding that can
    insert a `scope_locks` row outright. There is no provenance here that they
    could not equally forge, and the deleted lock rows leave no history to audit
    against, so this code does not claim to verify one.

    ELIGIBILITY IS THE LOCK'S OWN EXPIRY, AND NOTHING ELSE. A restored lock is a
    NEW row, so it must satisfy the ordinary contract: an entry whose
    `expires_at` has already passed is not restored, whatever its mode. This
    deliberately does NOT mirror `check_expired_locks`'s live-owner exemption,
    which protects a lock whose owner never stopped being live — a reaped
    session is by the system's own verdict exactly not that. Mirroring it was
    measured re-minting an exclusive lock thirty days past its TTL, which
    `_get_active_locks` then counted live and the sweep then never collected,
    because resurrection had made the owner active a few lines earlier. Nothing
    here consults session status, so no mutation a caller already applied can
    influence the answer.

    THE ORIGINAL CLOCK COMES BACK WITH THE CLAIM. `created_at` is taken from the
    journal, never defaulted to now, because the reaper scores staleness as the
    max of started_at, last_heartbeat and the newest lock/commit/decision: a
    restored lock stamped `now` would buy the session a fresh inactivity window
    it had not earned.

    FIVE WHOLE-JOURNAL REFUSALS, each fail-closed, each granting nothing:

      1. `was_auto_completed` false — only the REAPER's guess is disprovable by
         a heartbeat. An owner who said done said done, and `update_session`
         empties the journal on its way out so there is nothing left to replay.
      2. `bound_worker` set — #2741 refuses an identity-bound resurrection
         outright. Enforced HERE as well as in the caller: an assertion written
         in the callee and enforced only by its caller is the shape that
         regressed this code in the first place.
      3. `creator_uid` NULL — `cross_account` degrades to "any identified
         account may" on rows predating caller-identity binding, so a heartbeat
         on one cannot be attributed. The underlying legacy gap is #2763.
      4. The anchor moved — a pattern means something only inside the repo it
         was authored against, and an ordinary session can be re-anchored by
         PATCH while it is dead.
      5. Any entry fails the ordinary lock contract — `LockCreate` validation, a
         real mode, a parseable expiry within `lock_ttl_hours`. Invalid input
         fails DETERMINISTICALLY and as a whole, rather than 500ing on an
         unexpected type or silently dropping what it could not read.

    CLAIMED, NOT READ. The in-process claim turns a second restorer away before
    it opens a write; the compare-and-set against the exact bytes read is the
    backstop that keeps "at most one grant" true regardless. A claim is consumed
    exactly once: a refusal after claiming leaves an EMPTY journal, never a
    half-consumed one, and a refusal BEFORE claiming leaves the journal intact
    for the legitimate attempt that may still come.

    Partial restoration remains legitimate for two reasons only — a newer holder
    took the lane, or the entry's own TTL elapsed — and each lane says which.
    """
    from pydantic import ValidationError
    from sqlalchemy import update as sa_update

    # Refusals 1-3 need no journal content. They do EMPTY the journal, because
    # an entry that may never be restored must not stay armed.
    if not was_auto_completed:
        await _disarm_journal(db, session)
        return _refused(REFUSAL_OWNER_COMPLETED,
                        "the reaper's guess is the only completion a heartbeat disproves")
    if getattr(session, "bound_worker", ""):
        await _disarm_journal(db, session)
        return _refused(REFUSAL_IDENTITY_BOUND,
                        "an identity-bound session's authority ended with it (#2741)")
    if getattr(session, "creator_uid", None) is None:
        # Name the lanes BEFORE disarming: after _disarm_journal the only record
        # that this session was stripped of them is gone (#2760).
        lanes = _journalled_patterns(session.reaped_locks or "")
        await _disarm_journal(db, session)
        return _refused(REFUSAL_UNIDENTIFIED_OWNER,
                        "session predates caller-identity binding (creator_uid unset); see #2763",
                        lanes)

    raw = session.reaped_locks or ""
    if not raw:
        return RestorationOutcome(outcome=OUTCOME_NOTHING_JOURNALLED)

    result = await db.execute(
        sa_update(Session)
        .where(Session.id == session.id, Session.reaped_locks == raw)
        .values(reaped_locks="")
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        # Somebody else consumed this journal between our read and our write —
        # another PROCESS, since the claim excludes this one. This is the
        # backstop that keeps "at most one grant" true without that claim.
        #
        # The in-memory attribute is NOT cleared here. Clearing it before the
        # rowcount was checked left a LOSER holding a dirty ORM object, so any
        # caller that committed afterwards would flush a journal consumption it
        # had not won — the one write a losing contender must never perform.
        return RestorationOutcome(
            outcome=OUTCOME_CONCURRENT,
            detail="the journal was consumed by another restoration")
    session.reaped_locks = ""
    return await _grant_from_journal(db, session, raw)


async def _disarm_journal(db: AsyncSession, session: Session) -> None:
    """Empty the journal unconditionally. Used by the refusals that can never be
    followed by a legitimate restoration of the same entries."""
    from sqlalchemy import update as sa_update

    if not (session.reaped_locks or ""):
        return
    await db.execute(
        sa_update(Session).where(Session.id == session.id)
        .values(reaped_locks="").execution_options(synchronize_session=False))
    session.reaped_locks = ""


async def _grant_from_journal(
    db: AsyncSession, session: Session, raw: str,
) -> RestorationOutcome:
    """Validate a claimed journal whole, then grant what is still available.

    EVERY REFUSAL BELOW IS POST-CAS. `restore_reaped_locks` has already won the
    compare-and-set and emptied `sessions.reaped_locks` before calling this, so
    `raw` is the last copy of what this session held. A refusal here therefore
    ABANDONS restoration permanently, and each one records the journalled lanes
    as unresolved loss — otherwise the session comes back active, holding
    nothing, with `scope` still naming every lane and nothing to contradict it.
    That is a class invariant, not a property of any one refusal: a legacy row
    whose pattern the lock contract now rejects reaches it just as readily as a
    hand-edited journal. Where `raw` cannot be parsed the lane list is empty,
    which records nothing new and — because the caller MERGES — erases nothing.
    """
    from pydantic import ValidationError

    from ai_team_sync.schemas import LockCreate

    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        return _refused(REFUSAL_INVALID_JOURNAL, "journal is not readable JSON",
                        _journalled_patterns(raw))
    if not isinstance(envelope, dict):
        return _refused(REFUSAL_INVALID_JOURNAL, "journal envelope is not an object",
                        _journalled_patterns(raw))

    journalled_root = canonical_root(envelope.get("repo_root") or "")
    current_root = canonical_root(getattr(session, "repo_root", "") or "")
    if journalled_root != current_root:
        # The lanes are knowable here even though they are not grantable: the
        # envelope parsed, it simply describes another repo. Recording them keeps
        # the session from reading as though it still held them.
        return _refused(REFUSAL_ANCHOR_MOVED,
                        f"anchor moved since reap ({journalled_root or '<unanchored>'} -> "
                        f"{current_root or '<unanchored>'}); a pattern does not travel between repos",
                        _journalled_patterns(raw))

    entries = envelope.get("locks")
    if not isinstance(entries, list):
        return _refused(REFUSAL_INVALID_JOURNAL, "journal carries no lock list",
                        _journalled_patterns(raw))
    if not entries:
        return RestorationOutcome(outcome=OUTCOME_NOTHING_JOURNALLED)

    # VALIDATE EVERYTHING BEFORE GRANTING ANYTHING. A journal is all-or-nothing
    # on validity; only a live conflict or an elapsed expiry may reduce it.
    now = datetime.now(timezone.utc)
    ttl_ceiling = now + timedelta(hours=settings.lock_ttl_hours)
    validated: list[tuple[str, str, str, datetime, datetime | None]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return _refused(REFUSAL_INVALID_JOURNAL, "journal entry is not an object",
                            _journalled_patterns(raw))
        mode = entry.get("mode")
        if mode not in ("advisory", "exclusive"):
            return _refused(REFUSAL_INVALID_JOURNAL,
                            f"journal entry carries an unknown mode {mode!r}",
                            _journalled_patterns(raw))
        reason = entry.get("reason") or ""
        if not isinstance(reason, str):
            return _refused(REFUSAL_INVALID_JOURNAL, "journal entry reason is not text",
                            _journalled_patterns(raw))
        try:
            # The ordinary lock contract, applied exactly as POST /api/locks
            # applies it: a glob, not prose, within length.
            pattern = LockCreate(session_id=session.id, pattern=entry.get("pattern"),
                                 mode=mode, reason=reason).pattern
        except ValidationError as e:
            return _refused(REFUSAL_INVALID_JOURNAL,
                            f"journal entry fails the lock contract: {_first_error(e)}",
                            _journalled_patterns(raw))
        expires_at = _journal_time(entry.get("expires_at"))
        if expires_at is None:
            return _refused(REFUSAL_INVALID_JOURNAL,
                            "journal entry carries an unreadable expiry",
                            _journalled_patterns(raw))
        if expires_at > ttl_ceiling:
            # Only a hand-edited journal can hold one: a real lock's expiry comes
            # from the same TTL at creation. Restoration never mints time.
            return _refused(REFUSAL_INVALID_JOURNAL,
                            f"journal entry outlives lock_ttl_hours ({settings.lock_ttl_hours}h)",
                            _journalled_patterns(raw))
        created_at = _journal_time(entry.get("created_at"))
        if created_at is not None and created_at > now:
            return _refused(REFUSAL_INVALID_JOURNAL,
                            "journal entry claims to have been created in the future",
                            _journalled_patterns(raw))
        validated.append((pattern, mode, reason, expires_at, created_at))

    outcome = RestorationOutcome(outcome=OUTCOME_RESTORED)
    for pattern, mode, reason, expires_at, created_at in validated:
        if expires_at <= now:
            outcome.not_restored.append(pattern)
            outcome.not_restored_reasons.append(NOT_RESTORED_EXPIRED)
            continue
        # THE SAME ANCHOR STRING create_lock PASSES, not the canonical form.
        # `_cross_repo` compares this against each holder's RAW `repo_root` with
        # nothing but an rstrip, so normalising only our side made restoration
        # the one claim path whose two halves disagreed: a session anchored
        # '/srv//B' had its newcomer's exclusive lock skipped as "another repo"
        # and was granted a lane POST /api/locks refuses with 409, leaving two
        # exclusive holders. Restoration must never grant what the ordinary door
        # would refuse for the same state. Canonicalisation stays where it
        # answers a different question — has the session moved away from the
        # repo its journal names — in the anchor gate above.
        conflicts = await _check_scope_conflicts(
            db, [pattern], session.developer,
            repo_root=getattr(session, "repo_root", "") or "",
            exclude_session_id=session.id)
        if blocking_conflict(conflicts, mode):
            outcome.not_restored.append(pattern)
            outcome.not_restored_reasons.append(NOT_RESTORED_NEWER_HOLDER)
            continue
        lock = ScopeLock(session_id=session.id, pattern=pattern, mode=mode,
                         reason=reason, expires_at=expires_at,
                         authority_bearing=False)
        if created_at is not None:
            # The ORIGINAL age, so restoring a lane cannot reset the reaper's
            # activity clock and buy the session an unearned window.
            lock.created_at = created_at
        db.add(lock)
        await db.flush()
        outcome.restored.append(pattern)
    return outcome


def _journal_time(value: object) -> datetime | None:
    """Parse a journalled ISO timestamp as UTC-aware, or None if unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


async def resurrect_session(db: AsyncSession, session: Session) -> RestorationOutcome:
    """Bring a REAPER-completed session back, with the lane it was stripped of.

    THE HEARTBEAT IS THE ONLY DOOR (#2760). Restoration used to be reachable
    from `PATCH /api/sessions/{id}` as well, and putting an authority-granting
    operation inside a multi-field mutator produced three separate defects that
    all had the same shape — the other fields are applied AFTER it. A single
    PATCH carrying `repo_root` re-anchored the session after restoration had
    conflict-checked against the old anchor, leaving it holding a lane in a
    namespace it never worked in; a single PATCH carrying `summary` overwrote
    the marker that recorded the restoration it had just performed; and a
    pause/resume pair never matched the completed->active shape at all, so it
    revived the session with no locks and left the journal armed. `update_session`
    now refuses any such transition with 409 and changes nothing.

    The caller refuses an OWNER-completed session before arriving here. This
    reads `auto_completed` BEFORE clearing it, so restoration sees the reaper's
    verdict rather than the state this function is about to write.

    The event is NOT dispatched here: a notification adapter must not run inside
    the write transaction that holds the database lock. The caller dispatches
    `session.resurrected` after the commit, with the outcome returned here. The
    authority (the lock rows) and the durable record that explains it (the
    summary marker) are written together, in one transaction, and only the
    telling of it is deferred.
    """
    # RESTORE BEFORE MUTATING. A contender that had already flipped
    # status/completed_at/auto_completed would have to COMMIT those columns, and
    # under journal_mode=delete that commit blocks on the winner's write lock and
    # raises `database is locked` — the very 500 this seam exists to avoid.
    # Measured: the loser was correctly refused the restoration and then died on
    # its own UPDATE. Restoration consults no session status by design, so
    # running it first changes none of its answers, and a contender now leaves
    # the row untouched.
    was_auto_completed = bool(getattr(session, "auto_completed", False))
    outcome = await restore_reaped_locks(db, session, was_auto_completed=was_auto_completed)
    if outcome.lost_the_claim:
        logger.info("session %s: restoration already claimed by a concurrent request",
                    session.id)
        return outcome

    session.status = "active"
    session.completed_at = None
    session.auto_completed = False
    # WHAT IS STILL NOT HELD, written down rather than merely returned (#2760).
    # This request's caller may discard the response — the Stop hook and the MCP
    # both do — and `scope` still names the lane either way, so the only thing
    # that stops a refused lane reading as held authority is a durable record.
    # Written in the SAME transaction as the lock rows, so the board can never
    # see one without the other.
    #
    # MERGED, NOT REPLACED. This is authority STATE, not an event log: a lane
    # stays unheld until a live lock proves otherwise, and a lifecycle cycle that
    # adjudicated nothing must not erase what an earlier one established. Lanes
    # restored by THIS cycle are dropped from the carried state, because a lock
    # row now proves them held.
    # SUPPRESSION IS THE READER'S JOB, NOT A DELETION. An earlier version also
    # dropped every lane this cycle restored, which looked equivalent — the
    # read-side subtraction hides a lane while a live lock covers it — but it
    # threw the history away. Restoration deliberately does NOT mint a fresh TTL
    # (test_restoration_does_not_mint_a_fresh_ttl), so a restored lock carries
    # its ORIGINAL expiry and can die minutes later; with the pair deleted there
    # was then no negative state left, and `scope` alone granted the lane again
    # on a session holding nothing. Keeping the pair costs a row of JSON and
    # makes the loss reappear by itself the moment the proof expires.
    carried = _merge_unresolved(
        _parse_unresolved(getattr(session, "locks_not_restored", "")),
        list(zip(outcome.not_restored, outcome.not_restored_reasons)))
    session.locks_not_restored = json.dumps([[p, r] for p, r in carried])

    # The marker carries COUNTS AND AN OUTCOME ONLY. No pattern ever reaches
    # narrative text, so no glob syntax can terminate the marker early, break
    # #2445's de-duplication, or graft a fragment onto an operator's own words.
    if outcome.outcome == OUTCOME_REFUSED:
        note = f"[resurrected: locks not restored ({outcome.reason})]"
    elif outcome.outcome == OUTCOME_NOTHING_JOURNALLED:
        note = "[resurrected: no locks were journalled]"
    elif outcome.not_restored:
        note = (f"[resurrected: {len(outcome.restored)} lock(s) restored, "
                f"{len(outcome.not_restored)} not restored]")
    else:
        note = f"[resurrected: {len(outcome.restored)} lock(s) restored]"
    session.summary = replace_lifecycle_marker(session.summary, note)

    logger.warning(
        "session %s resurrected: it was auto-completed as silent but is alive "
        "(outcome: %s, restored: %d, not restored: %d)",
        session.id, outcome.outcome, len(outcome.restored), len(outcome.not_restored))
    return outcome


async def dispatch_resurrection(session: Session, outcome: RestorationOutcome) -> None:
    """Announce a resurrection AFTER its transaction committed.

    Kept out of the write transaction deliberately: `dispatch` can reach Slack
    or Telegram, and a webhook round-trip inside an open SQLite write
    transaction holds the whole-file lock for its duration.
    """
    await dispatch("session.resurrected", {
        "session_id": session.id,
        "developer": session.developer,
        "agent": session.agent,
        "outcome": outcome.outcome,
        # The patterns live HERE, where a consumer reads a list instead of
        # parsing prose.
        "locks_restored": outcome.restored,
        "locks_not_restored": outcome.not_restored,
        "not_restored_reasons": outcome.not_restored_reasons,
        "restoration_reason": outcome.reason,
        "restoration_detail": outcome.detail,
    })


def _first_error(exc) -> str:
    """The first pydantic message, flattened for a one-line refusal."""
    try:
        return str(exc.errors()[0].get("msg", exc))[:200]
    except Exception:
        return str(exc)[:200]


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

    # ONE AUTHORITY DOOR (#2760). `scope` is authority-bearing input twice over:
    # it becomes ScopeLock rows below, and the PreToolUse guard reads a scope
    # pattern as a claim in its own right. It therefore has to clear the SAME
    # contract POST /api/locks clears, by running LockCreate's validator rather
    # than by restating its rules here — two doors that normalise differently is
    # what let 'src/** ' be stored verbatim at creation, journalled with its
    # trailing space, and come back from restoration as the broader 'src/**'.
    #
    # Validated BEFORE the session row exists, so a refusal cannot leave a
    # session behind holding half-checked authority. `scope` is reused for both
    # writes (json.dumps below and the lock loop), so normalising it here is what
    # keeps session.scope and its locks spelling the same claim.
    if scope:
        scope = _validated_scope(scope)

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

    # ONE DOOR (#2760). A session the reaper stripped carries an armed journal,
    # and the only path that may spend it is the heartbeat — the one signal that
    # actually proves the process alive. Refused HERE, before a single field is
    # applied, so the refusal is atomic: status, repo_root, summary, scope and
    # description are all left exactly as they were, and the journal stays armed
    # for the heartbeat that may still come.
    #
    # It is not enough to match completed->active. `paused` is a running state
    # reachable from `completed` by the same PATCH, and pause/resume are live
    # surfaces (cli.py `ats session pause`, mcp/server.py pause_session /
    # resume_session): reap -> PATCH paused -> PATCH active revived the session
    # with zero locks and left the journal armed for nobody, which is the exact
    # board state this ticket exists to kill.
    if (body.status is not None and body.status != session.status
            and body.status in ("active", "paused")
            and journal_names_locks(getattr(session, "reaped_locks", ""))):
        raise HTTPException(409, detail={
            "error": "restoration_requires_heartbeat",
            "message": (
                f"session {session.id} was completed by the reaper and still holds an "
                f"unspent lock-restoration journal; revive it with "
                f"POST /api/sessions/{session.id}/heartbeat, which is the only path that "
                f"restores its locks. No field of this request was applied."),
            "session_id": session.id,
            "requested_status": body.status})

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

    # THE SAME DOOR AS CREATION (#2760). PATCH is the other supported writer of
    # an authority-bearing scope, and MCP extend_scope reaches it: extend_scope
    # takes its new locks through POST /api/locks (validated) and then writes the
    # MERGED scope back through here, so an unvalidated PATCH could persist a
    # spelling its own lock does not have. Validated up here, above every
    # mutation, so an invalid pattern leaves status, summary, scope, repo_root
    # and the lock rows exactly as they were.
    validated_scope = _validated_scope(list(body.scope)) if body.scope is not None else None

    if body.status is not None:
        if body.status == "completed":
            # The OWNER is speaking, so the reaper's guess stops being the live
            # account of this session — and its journal stops being replayable
            # (#2760). Before this, `auto_completed` was only ever cleared inside
            # the resurrect branch, so an owner completing a session the reaper
            # had already latched left the flag set: the heartbeat's
            # operator-completed refusal never fired, and a stray ping re-minted
            # an EXCLUSIVE lock from a journal written a whole work cycle
            # earlier, onto a session whose own summary said the lane was
            # released.
            session.auto_completed = False
            session.reaped_locks = ""
        session.status = body.status
        if body.status == "completed":
            session.completed_at = datetime.now(timezone.utc)
            # Release all locks
            for lock in session.locks:
                await db.delete(lock)
    if body.summary is not None:
        session.summary = body.summary
    if validated_scope is not None:
        session.scope = json.dumps(validated_scope)
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
    await db.refresh(session, ["locks"])

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
    restoration = None
    claim = None
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
        # THE CLAIM IS TAKEN HERE, by the handler that owns the transaction, and
        # held until that transaction has definitively committed or rolled back.
        # Restoration's writes and the commit that makes them real are one
        # protected region; splitting them left the SQLite write lock held with
        # the claim free, and a contender arriving there blocked for the driver's
        # full busy timeout and died with `database is locked` (#2760).
        claim = restoration_claim(session.id)
        if claim.locked():
            # A contender. It mutates nothing, writes nothing and takes no
            # database lock, so it cannot stall the winner or anything else.
            await db.rollback()
            session = (await db.execute(
                select(Session).where(Session.id == session_id).options(
                    selectinload(Session.locks), selectinload(Session.decisions),
                    selectinload(Session.commits)))).scalar_one()
            return _session_to_response(session, restoration=RestorationOutcome(
                outcome=OUTCOME_CONCURRENT,
                detail="another request holds this session's restoration claim"))
        await claim.acquire()

    try:
        if claim is not None:
            # Bring it back WITH ITS LOCKS, which is what this branch has always
            # claimed to do.
            restoration = await resurrect_session(db, session)

        session.last_heartbeat = datetime.now(timezone.utc)
        await db.commit()
        # Reload the lock collection explicitly: a CONCURRENT restorer in another
        # PROCESS may have granted the lane, and a response whose lock_count
        # predates that commit is the same false board state, one layer up.
        await db.refresh(session, ["locks"])
    except Exception:
        await db.rollback()
        raise
    finally:
        # Released only once the transaction is resolved, either way. A claim
        # leaked here would deadlock every later heartbeat for this session.
        if claim is not None:
            claim.release()

    if restoration is not None and not restoration.lost_the_claim:
        # After the commit, never inside it: a notification adapter can reach
        # Slack or Telegram, and a webhook round-trip inside an open SQLite
        # write transaction holds the whole-file lock for its duration.
        await dispatch_resurrection(session, restoration)
    return _session_to_response(session, restoration=restoration)

    session.last_heartbeat = datetime.now(timezone.utc)
    await db.commit()
    # Reload the lock collection explicitly: a CONCURRENT restorer may have
    # granted the lane in another transaction, and a response whose lock_count
    # predates that commit is the same false board state, one layer up.
    await db.refresh(session, ["locks"])
    if restoration is not None and not restoration.lost_the_claim:
        # After the commit, never inside it: a notification adapter can reach
        # Slack or Telegram, and a webhook round-trip inside an open SQLite
        # write transaction holds the whole-file lock for its duration.
        await dispatch_resurrection(session, restoration)
    return _session_to_response(session, restoration=restoration)
