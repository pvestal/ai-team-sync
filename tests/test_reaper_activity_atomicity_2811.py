"""#2811 extension — retirement is atomic with concurrent ACTIVITY, not only
with the heartbeat.

THE RACE (operator gate 2, 2026-10-14). The reaper judges a session idle from
max(created_at) of its locks, commits and decisions plus last_heartbeat, then
retires it. A lock granted 201 after those reads but before the retirement was
journalled and deleted with the session: a successful grant silently
invalidated. Reproduced on 9fccb02 and on the heartbeat-only CAS (31185128).

THE CONTRACT. For every kind of activity (lock grant, commit record, decision,
heartbeat) racing a retirement, exactly one of:
  * the activity succeeds and the session stays active, holding what it got;
  * the retirement succeeds and the activity is refused.
Owner completion racing the reaper ends owner-completed with one owner event.

HOW. Same file-backed SQLite harness as test_reaper_heartbeat_cas_2811 (real
endpoints, separate pooled connections). The reaper's session fires a hook
right after its first statement that reads the `decisions` table: the last of
its activity reads, after it has judged the session idle and before it
retires it. Every forced test asserts the interleaving actually happened.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.models import CommitRecord, CompletionOutbox, Decision, ScopeLock
from tests.test_reaper_heartbeat_cas_2811 import (  # noqa: F401  (fixtures)
    OWNER_TOKEN, PATTERN, _persisted, _silent_session_with_lock, file_engine, harness)

NEW_LOCK = "src/acquired_in_window.py"
KINDS = ["lock", "commit", "decision"]


class _ReaperProbe(AsyncSession):
    """Fires `hook` once, right after the `nth` statement mentioning `marker`."""

    hook = None
    marker = "decisions"
    nth = 1
    seen = 0
    fired = False

    async def _maybe_fire(self, statement):
        if self.hook is not None and self.marker in str(statement).lower():
            self.seen += 1
            if self.seen < self.nth:
                return
            hook, self.hook = self.hook, None
            self.fired = True
            await hook()

    async def execute(self, statement, *a, **kw):
        result = await super().execute(statement, *a, **kw)
        await self._maybe_fire(statement)
        return result

    async def scalar(self, statement, *a, **kw):
        result = await super().scalar(statement, *a, **kw)
        await self._maybe_fire(statement)
        return result


async def _act(client, sid, kind, n=0):
    if kind == "lock":
        return await client.post("/api/locks", json={
            "session_id": sid, "pattern": NEW_LOCK, "mode": "advisory", "reason": "2811"})
    if kind == "commit":
        return await client.post(f"/api/sessions/{sid}/commits", json={
            "session_id": sid, "commit_hash": f"{n:040x}", "message": "2811"})
    if kind == "decision":
        return await client.post("/api/decisions", json={
            "session_id": sid, "title": f"2811 {n}", "chosen": "x"})
    if kind == "heartbeat":
        return await client.post(f"/api/sessions/{sid}/heartbeat")
    raise AssertionError(kind)


async def _artifact_present(factory, sid, kind) -> bool:
    async with factory() as db:
        if kind == "lock":
            q = select(func.count()).select_from(ScopeLock).where(
                ScopeLock.session_id == sid, ScopeLock.pattern == NEW_LOCK)
        elif kind == "commit":
            q = select(func.count()).select_from(CommitRecord).where(CommitRecord.session_id == sid)
        else:
            q = select(func.count()).select_from(Decision).where(Decision.session_id == sid)
        return bool(await db.scalar(q))


async def _reap_with_probe(file_engine, hook, marker="decisions", nth=1):
    factory = async_sessionmaker(file_engine, class_=_ReaperProbe, expire_on_commit=False)
    async with factory() as db:
        db.hook, db.marker, db.nth = hook, marker, nth
        reaped = await auto_complete_stale_sessions(db)
        return reaped, db.fired


async def _outbox(factory, sid) -> list[str]:
    async with factory() as db:
        return sorted((await db.execute(
            select(CompletionOutbox.completion_reason)
            .where(CompletionOutbox.session_id == sid))).scalars().all())


def _ok(resp) -> bool:
    return 200 <= resp.status_code < 300


# ── the gate-2 regression: activity after the idle judgement ─────────────

@pytest.mark.parametrize("kind", KINDS)
async def test_activity_granted_after_the_idle_judgement_is_never_invalidated(
        harness, file_engine, kind):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    responses = []

    async def act_in_window():
        responses.append(await _act(client, sid, kind))

    reaped, fired = await _reap_with_probe(file_engine, act_in_window)

    assert fired, "the reaper never read decisions; the window was not reached"
    assert len(responses) == 1 and _ok(responses[0]), responses[0].text
    row, locks = await _persisted(factory, sid)
    # The activity succeeded, so the session must still be active and hold it.
    assert (reaped, row.status) == (0, "active"), (
        f"{kind} answered {responses[0].status_code} and the session was retired anyway: "
        f"locks={locks}, journal={row.reaped_locks}")
    assert await _artifact_present(factory, sid, kind)
    assert PATTERN in locks
    if kind == "lock":
        assert NEW_LOCK in locks and not row.reaped_locks


@pytest.mark.parametrize("kind", KINDS + ["heartbeat"])
async def test_activity_arriving_inside_the_retirement_decision_waits_and_is_refused(
        harness, file_engine, kind):
    """The window the serialization exists for: after the reaper has re-read the
    session's activity for the retirement decision, before that decision commits.
    The activity is launched as its own task and given a moment; it must not
    complete there. Afterwards only the two permitted outcomes may hold."""
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    task: list[asyncio.Task] = []
    finished_inside: list[bool] = []

    async def act_inside_the_decision():
        task.append(asyncio.create_task(_act(client, sid, kind)))
        done, _ = await asyncio.wait(task, timeout=1.0)
        finished_inside.append(bool(done))

    # 1st decisions read = the unserialized candidate pass; 2nd = the re-read
    # that the retirement decision is made from.
    reaped, fired = await _reap_with_probe(file_engine, act_inside_the_decision, nth=2)
    resp = await task[0]

    assert fired, "the reaper never re-read activity; the window was not reached"
    assert resp.status_code < 500, resp.text
    row, locks = await _persisted(factory, sid)
    assert finished_inside == [False], (
        f"{kind} completed ({resp.status_code}) inside the retirement decision")
    assert reaped == 1
    if kind == "heartbeat":
        # Ran after the retirement committed, so it is the #2760 resurrection:
        # the row is active again WITH its lane, and the answer matches it.
        body = resp.json()
        assert body["status"] == row.status == "active"
        assert PATTERN in locks and PATTERN in body["locks_restored"]
        return
    assert row.status == "completed"
    assert not _ok(resp), f"{kind} answered {resp.status_code} on a retired session"
    assert not await _artifact_present(factory, sid, kind)


@pytest.mark.parametrize("kind", KINDS)
async def test_control_activity_before_the_activity_reads(harness, file_engine, kind):
    """Activity that lands before the reaper reads anything per-session: both the
    old and new code see it. Proves the harness, not the fix."""
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    responses = []

    async def act_early():
        responses.append(await _act(client, sid, kind))

    reaped, fired = await _reap_with_probe(file_engine, act_early, marker="sessions")

    assert fired and _ok(responses[0]), responses[0].text
    row, _ = await _persisted(factory, sid)
    assert (reaped, row.status) == (0, "active")


# ── the request loaded a live session, retirement committed before it wrote ─

@pytest.mark.parametrize("kind,code", [("lock", 400), ("commit", 409), ("decision", 409)])
async def test_activity_that_loaded_a_live_session_is_refused_after_retirement(
        harness, kind, code):
    client, factory, arm = harness
    sid = await _silent_session_with_lock(factory)
    reaped = []

    async def reaper_wins_the_gap():
        async with factory() as db:
            reaped.append(await auto_complete_stale_sessions(db))

    arm(reaper_wins_the_gap)
    resp = await _act(client, sid, kind)

    assert reaped == [1], "the interleaving did not happen"
    row, _ = await _persisted(factory, sid)
    assert row.status == "completed"
    assert resp.status_code == code, resp.text
    assert not await _artifact_present(factory, sid, kind), \
        f"{kind} was written onto a retired session"


async def test_liveness_touch_that_loaded_active_never_stamps_a_retired_row(harness):
    """The app-wide liveness dependency is a last_heartbeat writer too."""
    import os
    from tests.test_reaper_heartbeat_cas_2811 import _HookedSession
    from ai_team_sync.liveness import touch_session_liveness

    _, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    before, _ = await _persisted(factory, sid)
    reaped = []

    async def reaper_wins_the_gap():
        async with factory() as other:
            reaped.append(await auto_complete_stale_sessions(other))

    async with factory() as db:
        assert isinstance(db, _HookedSession)
        db.after_first_statement = reaper_wins_the_gap
        touched = await touch_session_liveness(db, peer_uid=os.getuid(), session_id=sid)

    assert reaped == [1], "the interleaving did not happen"
    row, _ = await _persisted(factory, sid)
    assert row.status == "completed"
    assert touched is False
    assert row.last_heartbeat == before.last_heartbeat, "a retired row was stamped alive"


# ── retirement first: later activity is refused, not half-accepted ───────

@pytest.mark.parametrize("kind,code", [("lock", 400), ("commit", 409), ("decision", 409)])
async def test_activity_after_retirement_is_refused(harness, kind, code):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)
    async with factory() as db:
        assert await auto_complete_stale_sessions(db) == 1

    resp = await _act(client, sid, kind)

    assert resp.status_code == code, resp.text
    row, _ = await _persisted(factory, sid)
    assert row.status == "completed"
    assert not await _artifact_present(factory, sid, kind)


# ── unforced concurrency: only the two permitted outcomes ────────────────

@pytest.mark.parametrize("kind", KINDS + ["heartbeat"])
@pytest.mark.parametrize("round_", range(8))
async def test_concurrent_activity_and_sweep_reach_a_permitted_outcome(
        harness, kind, round_):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)

    async def sweep():
        async with factory() as db:
            return await auto_complete_stale_sessions(db)

    resp, reaped = await asyncio.gather(_act(client, sid, kind, round_), sweep())

    assert resp.status_code < 500, resp.text
    row, locks = await _persisted(factory, sid)
    if kind == "heartbeat":
        # A heartbeat that loses to the reaper resurrects (#2760); either way it
        # never answers "active" over a completed row.
        assert resp.json()["status"] == row.status
        return
    if _ok(resp):
        assert row.status == "active", f"{kind} succeeded but the session was retired"
        assert await _artifact_present(factory, sid, kind)
        if kind == "lock":
            assert NEW_LOCK in locks
    else:
        assert row.status == "completed" and reaped == 1
        assert not await _artifact_present(factory, sid, kind)


async def test_pause_that_loaded_active_never_revives_a_reaped_row(harness):
    """Status writers other than completion: PATCH paused loads the row, the
    reaper retires it in the gap. Applying 'paused' would revive a reaped
    session with zero locks past the #2760 refusals."""
    client, factory, arm = harness
    sid = await _silent_session_with_lock(factory)
    reaped = []

    async def reaper_wins_the_gap():
        async with factory() as db:
            reaped.append(await auto_complete_stale_sessions(db))

    arm(reaper_wins_the_gap)
    resp = await client.patch(f"/api/sessions/{sid}", json={"status": "paused"},
                              headers={"X-ATS-Approval-Token": OWNER_TOKEN})

    assert reaped == [1], "the interleaving did not happen"
    row, locks = await _persisted(factory, sid)
    assert row.status == "completed", f"PATCH {resp.status_code} revived a reaped row"
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "session_status_changed"


@pytest.mark.parametrize("round_", range(8))
async def test_pause_racing_the_sweep_reaches_a_permitted_outcome(harness, round_):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)

    async def sweep():
        async with factory() as db:
            return await auto_complete_stale_sessions(db)

    resp, reaped = await asyncio.gather(
        client.patch(f"/api/sessions/{sid}", json={"status": "paused"},
                     headers={"X-ATS-Approval-Token": OWNER_TOKEN}),
        sweep())

    row, locks = await _persisted(factory, sid)
    if resp.status_code == 200:
        assert (row.status, reaped, locks) == ("paused", 0, [PATTERN])
    else:
        assert resp.status_code == 409, resp.text
        assert (row.status, reaped) == ("completed", 1)


@pytest.mark.parametrize("round_", range(8))
async def test_owner_completion_racing_the_sweep(harness, round_):
    client, factory, _ = harness
    sid = await _silent_session_with_lock(factory)

    async def sweep():
        async with factory() as db:
            return await auto_complete_stale_sessions(db)

    resp, reaped = await asyncio.gather(
        client.patch(f"/api/sessions/{sid}", json={"status": "completed"},
                     headers={"X-ATS-Approval-Token": OWNER_TOKEN}),
        sweep())

    assert resp.status_code == 200, resp.text
    row, locks = await _persisted(factory, sid)
    # The owner's account is final whichever ran first, and recorded once.
    assert (row.status, row.auto_completed) == ("completed", False)
    events = await _outbox(factory, sid)
    assert events.count("owner_completed") == 1
    assert events.count("reaped") == reaped <= 1
    assert locks == []
