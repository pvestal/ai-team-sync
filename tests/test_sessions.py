"""Tests for session CRUD endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_session(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "agent": "claude-code",
        "scope": ["src/auth/**"],
        "description": "Refactoring auth",
        "branch": "feat/auth",
        "auto_lock": True,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["developer"] == "patrick"
    assert data["agent"] == "claude-code"
    assert data["scope"] == ["src/auth/**"]
    assert data["status"] == "active"
    assert data["lock_count"] == 1


@pytest.mark.asyncio
async def test_list_sessions(client):
    # Create two sessions
    await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/"],
        "auto_lock": False,
    })
    await client.post("/api/sessions", json={
        "developer": "sarah",
        "scope": ["tests/"],
        "auto_lock": False,
    })

    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_list_sessions_filter_by_status(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/"],
        "auto_lock": False,
    })
    session_id = resp.json()["id"]

    # Complete the session
    await client.patch(f"/api/sessions/{session_id}", json={"status": "completed"})

    # Only active sessions
    resp = await client.get("/api/sessions", params={"status": "active"})
    assert len(resp.json()) == 0

    # All sessions
    resp = await client.get("/api/sessions")
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_complete_session_releases_locks(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "scope": ["src/auth/**"],
        "auto_lock": True,
    })
    session_id = resp.json()["id"]
    assert resp.json()["lock_count"] == 1

    # Complete
    await client.patch(f"/api/sessions/{session_id}", json={
        "status": "completed",
        "summary": "Done refactoring",
    })

    # Locks should be gone
    resp = await client.get("/api/locks")
    assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_get_session_not_found(client):
    resp = await client.get("/api/sessions/nonexistent")
    assert resp.status_code == 404
