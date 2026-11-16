"""#2808: re-anchoring a session cannot launder an exclusive lane past a newer holder.

A ScopeLock row has no anchor of its own; it is read under its session's
repo_root. #3581 refused PATCH repo_root while the session holds a LIVE lock.
What it left open was the DORMANT row: an exclusive lock whose owner is paused
and silent is not live (a newer holder may take the lane), but one heartbeat
makes it live again -- under whatever root the session has by then.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.config import settings
from ai_team_sync.models import ScopeLock, Session

REPO_A, REPO_B = "/srv/repo-a-2808", "/srv/repo-b-2808"


async def _start(client, root, agent):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": agent, "scope": [], "repo_root": root})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], {"X-ATS-Approval-Token": resp.headers["X-ATS-Approval-Token"]}


async def _lock(client, sid, pattern="src/**", mode="exclusive"):
    return await client.post("/api/locks", json={
        "session_id": sid, "pattern": pattern, "mode": mode})


async def _make_dormant(db, sid):
    """Paused, silent past the heartbeat window, and its exclusive row past TTL:
    not live, not yet swept."""
    long_ago = datetime.now(timezone.utc) - timedelta(
        minutes=settings.session_heartbeat_timeout_minutes * 4, hours=1)
    row = (await db.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.status = "paused"
    row.last_heartbeat = long_ago
    for lk in (await db.execute(select(ScopeLock).where(ScopeLock.session_id == sid))).scalars():
        lk.expires_at = long_ago
    await db.commit()


async def _live_holders(client, pattern="src/**"):
    return sorted(l["session_id"] for l in (await client.get("/api/locks")).json()
                  if l["pattern"] == pattern and l["mode"] == "exclusive")


@pytest.mark.asyncio
async def test_a_dormant_exclusive_row_refuses_the_reanchor(client, db_session):
    a, a_tok = await _start(client, REPO_A, "claude-code:aaaa2808")
    assert (await _lock(client, a)).status_code == 201
    await _make_dormant(db_session, a)

    b, _ = await _start(client, REPO_A, "claude-code:bbbb2808")
    assert (await _lock(client, b)).status_code == 201, "A's dormant row blocks nobody"
    assert await _live_holders(client) == [b]

    moved = await client.patch(f"/api/sessions/{a}", json={"repo_root": REPO_B})
    assert moved.status_code == 409, moved.text
    detail = moved.json()["detail"]
    assert detail["error"] == "repo_root_change_with_live_locks"
    assert detail["locks"] == [] and detail["dormant_locks"] == ["src/**"]
    row = (await client.get(f"/api/sessions/{a}")).json()
    assert row["repo_root"] == REPO_A, "no field of the refused request was applied"


@pytest.mark.asyncio
async def test_the_reviewers_five_step_laundering_is_refused(client, db_session):
    """Delegation eff86389's reproduction: A loses src/** to B in repo A, moves to
    repo B, claims src/** there, moves back -- two exclusive holders in repo A."""
    a, _ = await _start(client, REPO_A, "claude-code:aaaa2808")
    b, _ = await _start(client, REPO_A, "claude-code:bbbb2808")
    assert (await _lock(client, b)).status_code == 201

    assert (await client.patch(f"/api/sessions/{a}", json={"repo_root": REPO_B})
            ).status_code == 200, "a session holding nothing may move"
    assert (await _lock(client, a)).status_code == 201, "repo B's src/** is free"

    back = await client.patch(f"/api/sessions/{a}", json={"repo_root": REPO_A})
    assert back.status_code == 409, back.text
    assert back.json()["detail"]["locks"] == ["src/**"]
    assert await _live_holders(client) == sorted([a, b])  # one per repo, as claimed
    rows = {s["id"]: s["repo_root"] for s in (await client.get("/api/sessions")).json()}
    assert rows[a] == REPO_B and rows[b] == REPO_A


@pytest.mark.asyncio
async def test_completing_in_the_same_request_still_moves_and_releases(client, db_session):
    a, tok = await _start(client, REPO_A, "claude-code:aaaa2808")
    assert (await _lock(client, a)).status_code == 201
    await _make_dormant(db_session, a)
    done = await client.patch(f"/api/sessions/{a}", headers=tok,
                              json={"repo_root": REPO_B, "status": "completed"})
    assert done.status_code == 200, done.text
    assert (await db_session.execute(select(ScopeLock).where(
        ScopeLock.session_id == a))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_session_with_no_rows_moves_freely(client):
    a, _ = await _start(client, REPO_A, "claude-code:aaaa2808")
    moved = await client.patch(f"/api/sessions/{a}", json={"repo_root": REPO_B + "/"})
    assert moved.status_code == 200 and moved.json()["repo_root"] == REPO_B


# ── Codex VERIFY c5fb3132 findings: every writer of repo_root, and the race ──


@pytest.mark.asyncio
async def test_lifecycle_adoption_cannot_reanchor_a_placeholder_holding_a_lock(client):
    """start_session adopting a SessionStart placeholder assigns a new repo_root
    to the EXISTING row, which keeps its lock rows: same refusal as PATCH."""
    from ai_team_sync.session_marker import AUTOREG_DESCRIPTION

    key = "lifecycle-2808-adopt"
    placeholder = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:cccc2808", "scope": [],
        "repo_root": REPO_A, "description": AUTOREG_DESCRIPTION, "lifecycle_key": key})
    assert placeholder.status_code == 201, placeholder.text
    pid = placeholder.json()["id"]
    token = {"X-ATS-Approval-Token": placeholder.headers["X-ATS-Approval-Token"]}
    assert (await _lock(client, pid)).status_code == 201  # a placeholder can hold a lock

    adopt = await client.post("/api/sessions", headers=token, json={
        "developer": "patrick", "agent": "claude-code:cccc2808", "scope": [],
        "repo_root": REPO_B, "description": "real work", "lifecycle_key": key,
        "adopt_session_id": pid})
    assert adopt.status_code == 409, adopt.text
    assert adopt.json()["detail"]["error"] == "repo_root_change_with_live_locks"
    assert adopt.json()["detail"]["locks"] == ["src/**"]
    row = (await client.get(f"/api/sessions/{pid}")).json()
    assert row["repo_root"] == REPO_A and row["description"] == AUTOREG_DESCRIPTION

    same_root = await client.post("/api/sessions", headers=token, json={
        "developer": "patrick", "agent": "claude-code:cccc2808", "scope": [],
        "repo_root": REPO_A, "description": "real work", "lifecycle_key": key,
        "adopt_session_id": pid})
    assert same_root.status_code == 200, "adoption without a move is unaffected"


@pytest.mark.asyncio
async def test_a_lock_committed_after_the_precheck_still_refuses_the_reanchor(
        client, monkeypatch):
    """The PATCH pre-check reads the rows as loaded; a lock created before the
    assignment must still be seen. The re-anchor now runs inside the acquisition
    guard (which POST /api/locks also takes) and re-reads the rows there."""
    from ai_team_sync.routers import sessions as sessions_router

    a, _ = await _start(client, REPO_A, "claude-code:aaaa2808")
    real_precheck = sessions_router._refuse_reanchor_with_live_locks
    raced = {}

    def precheck_then_race(session, body):
        real_precheck(session, body)  # passes: no rows yet
        raced["checked"] = True

    monkeypatch.setattr(sessions_router, "_refuse_reanchor_with_live_locks", precheck_then_race)
    real_guard = sessions_router.__dict__.get("acquisition_guard")
    from ai_team_sync.routers import locks as locks_router
    original_guard = locks_router.acquisition_guard

    import contextlib

    @contextlib.asynccontextmanager
    async def guard_after_a_rival_lock(db):
        if raced.get("checked") and not raced.get("locked"):
            raced["locked"] = True
            created = await _lock(client, a)  # commits through its own guard
            assert created.status_code == 201, created.text
        async with original_guard(db):
            yield

    monkeypatch.setattr(locks_router, "acquisition_guard", guard_after_a_rival_lock)
    moved = await client.patch(f"/api/sessions/{a}", json={"repo_root": REPO_B})
    assert raced.get("locked"), "the race was not staged"
    assert moved.status_code == 409, moved.text
    assert moved.json()["detail"]["locks"] == ["src/**"]
    assert (await client.get(f"/api/sessions/{a}")).json()["repo_root"] == REPO_A
    assert real_guard is None  # sessions imports the guard lazily from locks
