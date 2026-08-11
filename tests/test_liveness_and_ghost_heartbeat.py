"""Liveness from ordinary requests, and heartbeats that arrive for a dead session.

Both defects were observed live on 2026-08-10 in the ats.db sessions table:

  bc62c5e9  started 03:17:32  completed 03:39:55  last_heartbeat 03:43:49
            summary: "[auto-completed: silent >20m (heartbeat lost)]"

The session was reaped 22 minutes after it started while the agent was working
continuously, and then received a heartbeat FOUR MINUTES AFTER it was declared
dead. Two separate bugs:

1. LIVENESS IS BLIND TO MOST WORK. The heartbeat is a per-turn Stop hook, and the
   presence/lockcheck hooks only fire on Edit|Write|MultiEdit. An agent reading
   files, running commands and querying databases emits NOTHING, so the sessions
   doing the longest uninterrupted work look the most dead. config.py's own note
   ("keep comfortably above the client heartbeat cadence -- a per-turn Stop
   hook") assumes turns are short; a deep-work turn is unbounded.

2. A HEARTBEAT FOR A COMPLETED SESSION WAS SILENTLY ACCEPTED. It is the highest-
   signal event the server can receive -- proof the reaper was WRONG -- and it
   was written to a corpse and discarded. Observed on two separate sessions
   (3436c282 was heartbeated 2h19m after completion).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import ScopeLock, Session


def _utcnow():
    return datetime.now(timezone.utc)


def _silent(minutes: int) -> datetime:
    return _utcnow() - timedelta(minutes=minutes)


async def _go_silent(db_session, sid: str) -> datetime:
    """Backdate every activity signal past the fast cutoff.

    The reaper's activity = newest of started_at, last_heartbeat AND the newest
    lock/commit/decision. create_session also creates locks stamped NOW, so
    backdating only the session leaves it trivially 'active' and any reap
    assertion passes for the wrong reason.
    """
    when = _silent(settings.session_heartbeat_timeout_minutes + 10)
    sess = await db_session.get(Session, sid)
    sess.started_at = when
    sess.last_heartbeat = when
    locks = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all()
    for lock in locks:
        lock.created_at = when
    await db_session.commit()
    return when


# ── 1. ordinary requests prove liveness ────────────────────────────────────

@pytest.mark.asyncio
async def test_session_header_on_any_request_counts_as_liveness(client, db_engine):
    """A plain GET carrying the session header refreshes last_heartbeat.

    This is the whole point: the agent need not call /heartbeat explicitly, and
    read-only work now proves the process is alive.
    """
    created = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:test",
        "scope": ["src/**"], "description": "liveness",
    })).json()
    sid = created["id"]
    assert created["last_heartbeat"] is None

    r = await client.get("/api/sessions", headers={"X-ATS-Session-Id": sid})
    assert r.status_code == 200

    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["last_heartbeat"] is not None, "ordinary request did not prove liveness"


@pytest.mark.asyncio
async def test_presence_update_counts_as_liveness_for_that_agent(client):
    """The PostToolUse presence hook already identifies the session by agent."""
    created = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:presence",
        "scope": ["src/**"], "description": "presence liveness",
    })).json()
    sid = created["id"]

    r = await client.post("/api/presence", json={
        "developer": "patrick", "agent": "claude-code:presence",
        "files": ["src/a.py"], "intent": "editing",
    })
    assert r.status_code == 200

    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["last_heartbeat"] is not None


@pytest.mark.asyncio
async def test_agent_liveness_refreshes_every_session_that_agent_holds(client):
    """One process, several repos, several sessions — all of them are alive.

    Regression for a real reap: agent claude-code:15b5b559 held 956401fb (7
    locks, /opt/anime-studio) and 832684d4 (ai-team-sync). Liveness resolved
    by-agent to the NEWEST session only, so 956401fb starved and the reaper took
    it — with its locks — while the process was demonstrably working.
    """
    agent = "claude-code:multi"
    first = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": agent,
        "scope": ["repo_a/**"], "description": "repo A",
    })).json()["id"]
    second = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": agent,
        "scope": ["repo_b/**"], "description": "repo B",
    })).json()["id"]
    assert first != second

    r = await client.post("/api/presence", json={
        "developer": "patrick", "agent": agent,
        "files": ["repo_b/x.py"], "intent": "editing",
    })
    assert r.status_code == 200

    for sid, label in ((first, "older"), (second, "newer")):
        got = (await client.get(f"/api/sessions/{sid}")).json()
        assert got["last_heartbeat"] is not None, f"{label} session was left to starve"


@pytest.mark.asyncio
async def test_unknown_session_header_is_ignored_not_an_error(client):
    """Liveness must never wedge a request — a bad/stale header is a no-op."""
    r = await client.get("/api/sessions", headers={"X-ATS-Session-Id": "no-such-session"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_reaper_spares_a_session_kept_alive_by_ordinary_requests(client, db_session):
    """The integration proof — this is bc62c5e9's exact scenario, fixed.

    A session that heartbeated once, then went silent past the fast window, but
    is still issuing ordinary requests, must NOT be reaped.
    """
    created = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:deepwork",
        "scope": ["src/**"], "description": "long turn",
    })).json()
    sid = created["id"]

    # Backdate every signal past the fast cutoff, as if the last Stop hook were
    # ages ago and no lock/decision had been written since.
    await _go_silent(db_session, sid)

    # The agent is alive and working — it just isn't hitting Stop.
    await client.get("/api/sessions", headers={"X-ATS-Session-Id": sid})

    reaped = await auto_complete_stale_sessions(db_session)
    assert reaped == 0

    still = (await client.get(f"/api/sessions/{sid}")).json()
    assert still["status"] == "active", "a live, working session was reaped"


# ── 2. a heartbeat for a dead session ──────────────────────────────────────

@pytest.mark.asyncio
async def test_heartbeat_on_active_session_still_bumps(client):
    """Regression: the ordinary path is unchanged."""
    sid = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:hb",
        "scope": ["src/**"], "description": "hb",
    })).json()["id"]

    r = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert r.status_code == 200
    assert r.json()["last_heartbeat"] is not None
    assert r.json()["status"] == "active"


@pytest.mark.asyncio
async def test_heartbeat_resurrects_a_reaper_completed_session(client, db_session):
    """A heartbeat after an AUTO-completion means the reaper guessed wrong.

    The process is provably alive, so the session comes back rather than the
    agent being forced to mint a new id and lose continuity with its work.
    """
    sid = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:ghost",
        "scope": ["src/**"], "description": "ghost",
    })).json()["id"]

    await _go_silent(db_session, sid)

    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await client.get(f"/api/sessions/{sid}")).json()["status"] == "completed"

    r = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active", "a provably-live session was not resurrected"
    assert body["completed_at"] is None

    row = await db_session.get(Session, sid)
    await db_session.refresh(row)
    assert row.auto_completed is False
    assert "resurrected" in (row.summary or "").lower()


@pytest.mark.asyncio
async def test_heartbeat_on_an_operator_completed_session_is_refused(client, db_session):
    """A session the operator finished stays finished, and is NOT stamped.

    This is the half that must not become a resurrection loophole: 'I said I was
    done' outranks a late hook firing from a process that is shutting down.
    """
    sid = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:done",
        "scope": ["src/**"], "description": "done",
    })).json()["id"]

    done = await client.patch(f"/api/sessions/{sid}",
                              json={"status": "completed", "summary": "wrapped"})
    assert done.status_code == 200

    before = await db_session.get(Session, sid)
    await db_session.refresh(before)
    hb_before = before.last_heartbeat

    r = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert r.status_code == 409

    after = await db_session.get(Session, sid)
    await db_session.refresh(after)
    assert after.status == "completed"
    assert after.last_heartbeat == hb_before, "a corpse was stamped"


@pytest.mark.asyncio
async def test_reaper_marks_auto_completed_operator_complete_does_not(client, db_session):
    """The flag that separates the two cases above is set only by the reaper."""
    reaped_id = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:r",
        "scope": ["a/**"], "description": "r",
    })).json()["id"]
    manual_id = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:m",
        "scope": ["b/**"], "description": "m",
    })).json()["id"]

    await _go_silent(db_session, reaped_id)
    assert await auto_complete_stale_sessions(db_session) == 1

    await client.patch(f"/api/sessions/{manual_id}", json={"status": "completed"})

    reaped = await db_session.get(Session, reaped_id)
    manual = await db_session.get(Session, manual_id)
    await db_session.refresh(reaped)
    await db_session.refresh(manual)
    assert reaped.auto_completed is True
    assert manual.auto_completed is False
