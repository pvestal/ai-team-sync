"""Tower #2756 (ATS Gap 4): POST /api/locks applies the overlap rule that
session creation applies.

create_lock used to insert unconditionally, so a session could lay a lock over
another live session's exclusive claim that start_session would have refused.
"""

from __future__ import annotations

import pytest

REPO = "/srv/gap4-repo"
OTHER_REPO = "/srv/gap4-other"


async def _session(client, scope=(), mode="advisory", repo_root=REPO):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "default", "scope": list(scope),
        "auto_lock": True, "lock_mode": mode, "repo_root": repo_root})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _lock(client, sid, pattern, mode):
    return await client.post("/api/locks", json={"session_id": sid, "pattern": pattern, "mode": mode})


async def _patterns_of(client, sid):
    return {lock["pattern"] for lock in (await client.get("/api/locks")).json()
            if lock["session_id"] == sid}


@pytest.mark.asyncio
async def test_an_overlapping_exclusive_lock_of_another_session_is_refused(client):
    holder = await _session(client, ["src/auth/**"], mode="exclusive")
    other = await _session(client)

    resp = await _lock(client, other, "src/auth/jwt.py", "exclusive")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "scope_conflict"
    assert detail["message"].startswith("Cannot create lock: scope 'src/auth/jwt.py' conflicts")
    assert [c["session_id"] for c in detail["conflicts"]] == [holder]
    assert await _patterns_of(client, other) == set(), "a refused lock must not be written"


@pytest.mark.asyncio
async def test_an_advisory_lock_into_another_sessions_exclusive_lock_is_refused(client):
    await _session(client, ["src/auth/**"], mode="exclusive")
    other = await _session(client)
    assert (await _lock(client, other, "src/auth/jwt.py", "advisory")).status_code == 409


@pytest.mark.asyncio
async def test_an_exclusive_lock_over_another_sessions_advisory_lock_is_refused(client):
    # Session creation's rule: an exclusive claim cannot coexist with any overlap.
    await _session(client, ["src/auth/**"], mode="advisory")
    other = await _session(client)
    assert (await _lock(client, other, "src/auth/jwt.py", "exclusive")).status_code == 409


@pytest.mark.asyncio
async def test_advisory_locks_still_share(client):
    await _session(client, ["src/auth/**"], mode="advisory")
    other = await _session(client)
    assert (await _lock(client, other, "src/auth/jwt.py", "advisory")).status_code == 201


@pytest.mark.asyncio
async def test_a_non_overlapping_lock_is_allowed(client):
    await _session(client, ["src/auth/**"], mode="exclusive")
    other = await _session(client)
    assert (await _lock(client, other, "src/models/**", "exclusive")).status_code == 201


@pytest.mark.asyncio
async def test_the_same_path_in_a_different_repo_does_not_conflict(client):
    await _session(client, ["src/auth/**"], mode="exclusive", repo_root=REPO)
    other = await _session(client, repo_root=OTHER_REPO)
    assert (await _lock(client, other, "src/auth/**", "exclusive")).status_code == 201


@pytest.mark.asyncio
async def test_an_unanchored_session_still_conflicts_conservatively(client):
    await _session(client, ["src/auth/**"], mode="exclusive", repo_root=REPO)
    legacy = await _session(client, repo_root="")
    assert (await _lock(client, legacy, "src/auth/jwt.py", "exclusive")).status_code == 409


@pytest.mark.asyncio
async def test_a_session_may_extend_its_own_exclusive_scope(client):
    me = await _session(client, ["src/auth/**"], mode="exclusive")
    assert (await _lock(client, me, "src/auth/jwt.py", "exclusive")).status_code == 201
    assert (await _lock(client, me, "src/auth/**", "advisory")).status_code == 201
    assert (await _lock(client, me, "src/other/**", "exclusive")).status_code == 201


@pytest.mark.asyncio
async def test_a_completed_holder_no_longer_conflicts(client):
    holder = await _session(client, ["src/auth/**"], mode="exclusive")
    other = await _session(client)
    assert (await _lock(client, other, "src/auth/jwt.py", "exclusive")).status_code == 409
    assert (await client.patch(f"/api/sessions/{holder}", json={"status": "completed"})).status_code == 200
    assert (await _lock(client, other, "src/auth/jwt.py", "exclusive")).status_code == 201


@pytest.mark.asyncio
async def test_session_creation_refuses_with_the_same_contract(client):
    await _session(client, ["src/auth/**"], mode="exclusive")
    resp = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "default", "scope": ["src/auth/jwt.py"],
        "auto_lock": True, "repo_root": REPO})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "scope_conflict"
    assert detail["message"] == ("Cannot create session: scope 'src/auth/jwt.py' conflicts with "
                                 "exclusive lock 'src/auth/**' held by patrick")
