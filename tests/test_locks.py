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
