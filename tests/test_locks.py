"""Tests for scope lock endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_check_lock_conflict(client):
    # Create a session with a lock
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/auth/**"],
        "auto_lock": True,
    })
    assert resp.status_code == 201

    # Check a path that matches the lock
    resp = await client.post("/api/locks/check", json={
        "paths": ["src/auth/middleware.py"],
    })
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["locked"] is True
    assert results[0]["developer"] == "patrick"
    assert results[0]["pattern"] == "src/auth/**"


@pytest.mark.asyncio
async def test_check_no_conflict(client):
    # Create a session with a lock on auth
    await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/auth/**"],
        "auto_lock": True,
    })

    # Check a path outside the lock scope
    resp = await client.post("/api/locks/check", json={
        "paths": ["src/models/user.py"],
    })
    results = resp.json()
    assert len(results) == 1
    assert results[0]["locked"] is False


@pytest.mark.asyncio
async def test_list_locks(client):
    await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/auth/**", "src/middleware/**"],
        "auto_lock": True,
    })

    resp = await client.get("/api/locks")
    assert resp.status_code == 200
    locks = resp.json()
    assert len(locks) == 2
    # Every lock must carry its id so a stale/ghost lock is reapable via
    # delete_lock(lock_id) — list_all_locks surfaces this id to agents.
    # (ats-reap-stale-locks-needs-lock-ids-p01)
    assert all(lock.get("id") for lock in locks)


@pytest.mark.asyncio
async def test_lock_reason_roundtrips_and_surfaces_in_check(client):
    # A lock carries a human-readable reason; pattern stays a real glob.
    sresp = await client.post("/api/sessions", json={"developer": "patrick", "auto_lock": False})
    sid = sresp.json()["id"]
    lresp = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "packages/scene_generation/builder.py",
        "mode": "exclusive", "reason": "modularizing builder.py (#1512)",
    })
    assert lresp.status_code == 201
    assert lresp.json()["reason"] == "modularizing builder.py (#1512)"

    # check surfaces the reason so a blocked agent knows WHY
    cresp = await client.post("/api/locks/check", json={"paths": ["packages/scene_generation/builder.py"]})
    r = cresp.json()[0]
    assert r["locked"] is True
    assert r["reason"] == "modularizing builder.py (#1512)"
    # check_locks must surface lock_id so a blocked agent can reap a stale lock.
    assert r["lock_id"]


@pytest.mark.asyncio
async def test_prose_pattern_is_rejected(client):
    # The exact anti-pattern: a sentence as the lock pattern. Must 422 (it would
    # never fnmatch a real path -> a silent no-op lock).
    sresp = await client.post("/api/sessions", json={"developer": "patrick", "auto_lock": False})
    sid = sresp.json()["id"]
    resp = await client.post("/api/locks", json={
        "session_id": sid,
        "pattern": "composition routing consolidation: wire total_body_count into select_keyframe_method",
        "mode": "advisory",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_valid_globs_accepted(client):
    sresp = await client.post("/api/sessions", json={"developer": "patrick", "auto_lock": False})
    sid = sresp.json()["id"]
    for pat in ("src/**", "packages/foo/bar.py", "*.py"):
        resp = await client.post("/api/locks", json={"session_id": sid, "pattern": pat})
        assert resp.status_code == 201, f"{pat} should be a valid glob"


@pytest.mark.asyncio
async def test_delete_lock(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/auth/**"],
        "auto_lock": True,
    })

    locks_resp = await client.get("/api/locks")
    lock_id = locks_resp.json()[0]["id"]

    resp = await client.delete(f"/api/locks/{lock_id}")
    assert resp.status_code == 204

    locks_resp = await client.get("/api/locks")
    assert len(locks_resp.json()) == 0
