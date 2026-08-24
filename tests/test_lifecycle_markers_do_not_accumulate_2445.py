"""Reap/resurrect bookkeeping must not eat the summary field (#2445).

MEASURED 2026-08-14 across the live session table (1116 rows): 16 sessions carried
reap/resurrect thrash, 55 resurrect events total, worst single session 10 cycles. One
session had 3 markers and 281 characters of machine chatter in `summary` before any
handoff was written into it.

THE LOOP. The reaper completes a session after the heartbeat window; the next heartbeat
resurrects it; each half APPENDS to `summary`:

    [auto-completed: silent >20m (heartbeat lost)] [resurrected: heartbeat proved the
    reap wrong] [auto-completed: silent >20m (heartbeat lost)] [resurrected: ...]

A long-running interactive session is silent for >20 min routinely -- the operator is
reading, or one tool call is slow -- so this is the NORMAL case, not an edge.

`summary` is where a human writes a wrap note. Sharing it with unbounded machine chatter
makes a real handoff unfindable.

WHAT THIS FIXES AND WHAT IT DELIBERATELY DOES NOT. The fix is IDEMPOTENCE, not silence:
exactly one lifecycle marker survives in `summary`, so #2554's contract that a reaped
session SAYS SO is preserved (tests/test_reaper_names_stranded_work_2554.py asserts
"auto-completed" is present, and its absence on a clean session is meaningful). What
changes is that cycle N+1 REPLACES cycle N's marker instead of appending to it.

No new column: the full history already exists as dispatched events
(`session.auto_completed`, `session.resurrected`), which is the event log the ticket's
fix (b) asks for. Adding a lifecycle_log column would duplicate it.

The STRANDED note is NOT a lifecycle marker and must survive untouched -- it is
actionable, human-facing, and #2554 put it in `summary` deliberately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import Session


def _utcnow():
    return datetime.now(timezone.utc)


def _stale_session(**kw) -> Session:
    stale = _utcnow() - timedelta(hours=settings.session_inactivity_hours + 1)
    base = dict(developer="patrick", agent="claude-code", status="active",
                started_at=stale, repo_root="", scope="[]")
    base.update(kw)
    return Session(**base)


async def _reap(db_session, sess) -> Session:
    db_session.add(sess)
    await db_session.commit()
    await auto_complete_stale_sessions(db_session)
    return (await db_session.execute(
        select(Session).where(Session.id == sess.id))).scalar_one()


@pytest.mark.asyncio
async def test_a_second_reap_replaces_the_first_marker_rather_than_appending(db_session):
    """The thrash loop's actual harm: N cycles left N markers."""
    sess = _stale_session()
    row = await _reap(db_session, sess)
    assert "auto-completed" in (row.summary or ""), "#2554's contract still holds"
    first = row.summary

    # Simulate the next cycle: the session went active again and fell silent again.
    row.status = "active"
    row.completed_at = None
    row.started_at = _utcnow() - timedelta(hours=settings.session_inactivity_hours + 1)
    await db_session.commit()
    await auto_complete_stale_sessions(db_session)
    row = (await db_session.execute(
        select(Session).where(Session.id == sess.id))).scalar_one()

    assert row.summary.count("[auto-completed:") == 1, (
        f"markers accumulated across cycles: {row.summary!r}")
    assert len(row.summary) <= len(first) + 1, "summary grew across a reap cycle"


@pytest.mark.asyncio
async def test_an_operator_handoff_survives_being_reaped(db_session):
    """The whole point of the field: a human wrap note must remain readable."""
    handoff = ("Landed the depth-ref fix; benched 3 shots on seed 358134626; "
               "next step is the operator's eye on 8b9a3210.")
    sess = _stale_session(summary=handoff)

    row = await _reap(db_session, sess)

    assert handoff in row.summary, "the operator's narrative was destroyed"
    assert "auto-completed" in row.summary


@pytest.mark.asyncio
async def test_repeated_cycles_keep_the_handoff_readable(db_session):
    """Ten cycles is the worst case actually observed in the live table."""
    handoff = "HANDOFF: see ticket 2445 for the reaper thrash measurement."
    sess = _stale_session(summary=handoff)
    row = await _reap(db_session, sess)

    for _ in range(9):
        row.status = "active"
        row.completed_at = None
        row.started_at = _utcnow() - timedelta(
            hours=settings.session_inactivity_hours + 1)
        await db_session.commit()
        await auto_complete_stale_sessions(db_session)
        row = (await db_session.execute(
            select(Session).where(Session.id == sess.id))).scalar_one()

    assert handoff in row.summary
    assert row.summary.count("[auto-completed:") == 1, (
        f"10 cycles left {row.summary.count('[auto-completed:')} markers")
