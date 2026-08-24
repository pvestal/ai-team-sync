"""Background tasks for ai-team-sync server."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.config import settings
from ai_team_sync.database import get_db
from ai_team_sync.git_utils import uncommitted_for_scope
from ai_team_sync.models import CommitRecord, Decision, OverrideRequest, ScopeLock, Session
from ai_team_sync.events import broadcast_event

# Lifecycle bookkeeping the reaper/resurrect pair writes into `summary`. Matched so a
# NEW marker can REPLACE the previous one instead of appending to it (#2445).
#
# WHY REPLACE RATHER THAN APPEND. A long-running interactive session is routinely silent
# for longer than the heartbeat window (the operator is reading, or one tool call is
# slow), so reap -> heartbeat -> resurrect is the NORMAL cycle, not an edge. Each half
# used to append, so N cycles left N markers: measured 2026-08-14 over 1116 live rows,
# 16 sessions carried thrash, 55 resurrect events, worst single session 10 cycles, one
# with 281 characters of machine chatter before any handoff was written. `summary` is
# where a human writes the wrap note, and it was being consumed by bookkeeping.
#
# NOT SILENCED, DE-DUPLICATED: exactly one current marker survives, so #2554's contract
# that a reaped session SAYS SO still holds and its ABSENCE on a clean session still
# means something. The full history is already available as dispatched events
# (session.auto_completed / session.resurrected), which is the event log to read for
# per-cycle detail — a dedicated column would duplicate it.
#
# STRANDED is matched too because it is re-derived on every reap: the new note carries a
# fresh file list, so a stale one must not linger beside it.
_LIFECYCLE_MARKER_RE = re.compile(
    r"\s*\[(?:auto-completed|resurrected|STRANDED)\b[^\]]*\]")


def replace_lifecycle_marker(summary: str | None, note: str) -> str:
    """Return `summary` with prior lifecycle markers dropped and `note` appended.

    Operator narrative is preserved verbatim; only the machine markers are rewritten.
    """
    base = _LIFECYCLE_MARKER_RE.sub("", summary or "").strip()
    return f"{base} {note}".strip() if base else note


async def check_expired_locks(db: AsyncSession) -> int:
    """Notify and DELETE expired locks. Returns the number swept.

    Rewritten 2026-06-24: the old version (a) had a real bug — it filtered with
    ``datetime.replace(minute=now.minute - 1)`` which raises ValueError at minute 0
    and is logically meaningless — and (b) only broadcast an event, never deleting
    the lock. Expired locks therefore accumulated as DB cruft forever. Now every
    lock with ``expires_at <= now`` is notified once and removed, so the board
    reflects reality and stale locks stop lingering (up to the 8h TTL was the
    symptom behind 'why are there stale locks').
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScopeLock, Session).join(Session).where(ScopeLock.expires_at <= now)
    )
    expired = list(result.all())
    for lock, session in expired:
        await broadcast_event(session.id, "lock.expired", {
            "lock_id": lock.id,
            "pattern": lock.pattern,
            "mode": lock.mode,
            "expired_at": lock.expires_at.isoformat(),
        })
        await db.delete(lock)
    if expired:
        await db.commit()
    return len(expired)


async def expire_stale_override_requests(db: AsyncSession) -> int:
    """Flip pending override-requests past their response window to 'expired'.

    The model sets a 15-min ``expires_at`` but nothing ever enforced it, so a
    requester could wait forever on a dead request. Returns the number expired.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(OverrideRequest)
        .where(OverrideRequest.status == "pending")
        .where(OverrideRequest.expires_at <= now)
    )
    stale = list(result.scalars().all())
    for req in stale:
        req.status = "expired"
        req.responded_at = now
        await broadcast_event(req.requester_session_id, "override.expired", {
            "request_id": req.id,
            "conflicting_pattern": req.conflicting_pattern,
        })
    if stale:
        await db.commit()
    return len(stale)


async def auto_complete_stale_sessions(db: AsyncSession) -> int:
    """Auto-complete 'active' sessions that have gone silent, so their locks don't
    linger and hold the lane after the agent's process is gone.

    Activity = the most recent of: session.started_at, its newest lock/commit/decision,
    and last_heartbeat. Two windows:

    * FAST path — only for sessions that have EVER heartbeated (last_heartbeat set):
      if silent for > session_heartbeat_timeout_minutes (default 20), complete. A live
      client heartbeats every turn, so silence this long means the process is gone.
    * FALLBACK — sessions that never heartbeated (legacy/other clients): the
      session_inactivity_hours window (default 4h). So adding heartbeats is strictly
      never-worse: heartbeating sessions get fast cleanup, everything else behaves as
      a (shorter) version of before. See docs/product-gaps-reaper-and-scope.md Gap 1.
    """
    def _aware(dt):
        # SQLite returns tz-naive datetimes even for timezone=True columns; assume UTC.
        return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt

    now = datetime.now(timezone.utc)
    slow_cutoff = now - timedelta(hours=settings.session_inactivity_hours)
    fast_cutoff = now - timedelta(minutes=settings.session_heartbeat_timeout_minutes)
    result = await db.execute(select(Session).where(Session.status == "active"))
    completed = 0
    # One `git status` per repo_root across the whole sweep, not per session —
    # sessions cluster on the same few repos.
    uncommitted_cache: dict[str, list[str]] = {}
    for sess in result.scalars().all():
        last_lock = await db.scalar(
            select(func.max(ScopeLock.created_at)).where(ScopeLock.session_id == sess.id))
        last_commit = await db.scalar(
            select(func.max(CommitRecord.created_at)).where(CommitRecord.session_id == sess.id))
        last_decision = await db.scalar(
            select(func.max(Decision.created_at)).where(Decision.session_id == sess.id))
        last_activity = max(
            _aware(t) for t in
            (sess.started_at, last_lock, last_commit, last_decision, sess.last_heartbeat) if t)

        heartbeated = sess.last_heartbeat is not None
        cutoff = fast_cutoff if heartbeated else slow_cutoff
        if last_activity < cutoff:
            # LOOK BEFORE COMPLETING (#2554). Reaping drops the session's locks,
            # and anything it left dirty in a shared repo becomes unattributed:
            # the next session's `git add -A` sweeps it into an unrelated commit,
            # or a checkout destroys it. Measured 2026-08-23 on /opt/anime-studio
            # — four reaped sessions had left 10 uncommitted files (four real
            # fixes plus their passing tests) and NOTHING surfaced them.
            #
            # This has to happen BEFORE the status flip, because the answer is
            # unknowable afterwards: a live `git status` on a session completed
            # hours ago reports whoever is dirty in that repo NOW.
            #
            # We still reap. Holding a dead session's locks is worse than
            # reaping it — the lane stays blocked for everyone. What changes is
            # that the stranded work is NAMED, in the summary (durable) and on
            # the event (live), instead of vanishing silently.
            stranded: list[str] = []
            try:
                scope = json.loads(sess.scope) if sess.scope else []
                stranded = uncommitted_for_scope(
                    getattr(sess, "repo_root", "") or "", scope, uncommitted_cache)
            except Exception:  # noqa: BLE001 — never let the sweep die on a git call
                stranded = []

            sess.status = "completed"
            sess.completed_at = now
            # Mark WHO completed it. A later heartbeat on a reaper-completed
            # session is proof this decision was wrong and resurrects it; the
            # same heartbeat on an operator-completed session must not.
            sess.auto_completed = True
            reason = (f"silent >{settings.session_heartbeat_timeout_minutes}m (heartbeat lost)"
                      if heartbeated else
                      f"inactive >{settings.session_inactivity_hours}h")
            note = f"[auto-completed: {reason}]"
            if stranded:
                note += (f" [STRANDED {len(stranded)} uncommitted file(s) in scope: "
                         + ", ".join(stranded) + "]")
            sess.summary = replace_lifecycle_marker(sess.summary, note)
            await broadcast_event(sess.id, "session.auto_completed", {
                "session_id": sess.id, "last_activity": last_activity.isoformat(),
                "reason": reason, "uncommitted_in_scope": stranded})
            completed += 1
    if completed:
        await db.commit()
    return completed


async def run_cleanup_once(db: AsyncSession) -> dict[str, int]:
    """One sweep: expired locks + stale override-requests + stale sessions.

    Shared by the periodic loop AND the explicit startup sweep so a server restart
    immediately reclaims sessions/locks orphaned while it was down (a crashed server
    leaves every session 'active' in the DB; live clients re-heartbeat within a turn,
    dead ones are caught here on the way up instead of lingering)."""
    return {
        "locks_expired": await check_expired_locks(db),
        "override_requests_expired": await expire_stale_override_requests(db),
        "sessions_completed": await auto_complete_stale_sessions(db),
    }


async def run_startup_cleanup() -> dict[str, int]:
    """Explicit one-shot sweep at server startup (see run_cleanup_once)."""
    async for db in get_db():
        result = await run_cleanup_once(db)
        if any(result.values()):
            print(f"[ats] startup cleanup: {result}")
        return result
    return {}


async def cleanup_task_loop():
    """Background loop: sweep expired locks + stale override-requests + stale sessions."""
    while True:
        try:
            async for db in get_db():
                await run_cleanup_once(db)
        except Exception as e:
            print(f"Error in cleanup task: {e}")

        # Wait 1 minute before next check
        await asyncio.sleep(60)


async def start_background_tasks():
    """Run an immediate startup sweep, then start the periodic cleanup loop."""
    await run_startup_cleanup()
    asyncio.create_task(cleanup_task_loop())
