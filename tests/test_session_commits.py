"""POST /api/sessions/{id}/commits — the route the post-commit hook calls.

The hook (hooks/post_commit.py) shipped in the initial scaffold posting here,
but the route was never written: every call 404'd, the hook swallowed it, and
commit_count stayed 0 forever. CommitRecord.created_at also feeds the reaper's
last-activity check, so a session that only committed looked idle.
"""

from __future__ import annotations

import os

import pytest

HASH_A = "a" * 40
HASH_B = "b" * 40


async def _new_session(client) -> str:
    resp = await client.post("/api/sessions", json={
        "agent": "default", "developer": "patrick", "scope": ["src/"], "auto_lock": False})
    assert resp.status_code == 201
    return resp.json()["id"]


def _hook_body(session_id: str, commit_hash: str, message: str = "fix: thing") -> dict:
    # Exactly the payload post_commit.main() sends.
    return {"session_id": session_id, "commit_hash": commit_hash, "message": message}


@pytest.mark.asyncio
async def test_hook_payload_records_commit_and_count_rises(client):
    sid = await _new_session(client)
    assert (await client.get(f"/api/sessions/{sid}")).json()["commit_count"] == 0

    resp = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, HASH_A))
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"] == sid
    assert data["commit_hash"] == HASH_A
    assert data["message"] == "fix: thing"

    await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, HASH_B))
    assert (await client.get(f"/api/sessions/{sid}")).json()["commit_count"] == 2


@pytest.mark.asyncio
async def test_same_hash_twice_is_one_record(client):
    sid = await _new_session(client)
    first = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, HASH_A))
    again = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, HASH_A))
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert (await client.get(f"/api/sessions/{sid}")).json()["commit_count"] == 1


@pytest.mark.asyncio
async def test_unknown_session_is_404(client):
    resp = await client.post("/api/sessions/nope/commits", json=_hook_body("nope", HASH_A))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_body_session_id_must_match_path(client):
    sid = await _new_session(client)
    resp = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body("other", HASH_A))
    assert resp.status_code == 422
    assert (await client.get(f"/api/sessions/{sid}")).json()["commit_count"] == 0


@pytest.mark.asyncio
async def test_malformed_hash_is_422(client):
    sid = await _new_session(client)
    resp = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, "not-a-sha"))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_completed_session_refuses_commit(client):
    """A stale ~/.ats_session pointer must not keep feeding a finished session."""
    created = await client.post("/api/sessions", json={
        "agent": "default", "developer": "patrick", "scope": ["src/"], "auto_lock": False})
    sid = created.json()["id"]
    owner = {"X-ATS-Approval-Token": created.headers["X-ATS-Approval-Token"]}
    done = await client.patch(f"/api/sessions/{sid}", json={"status": "completed"}, headers=owner)
    assert done.status_code == 200, done.text
    resp = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, HASH_A))
    assert resp.status_code == 409
    assert (await client.get(f"/api/sessions/{sid}")).json()["commit_count"] == 0


@pytest.mark.asyncio
async def test_other_account_cannot_record_commit(client, monkeypatch):
    """Commit time counts as liveness for the reaper, so it is owner-only (#2741)."""
    sid = await _new_session(client)
    from ai_team_sync import peer_identity
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: os.getuid() + 1)
    resp = await client.post(f"/api/sessions/{sid}/commits", json=_hook_body(sid, HASH_A))
    assert resp.status_code == 403
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: os.getuid())
    assert (await client.get(f"/api/sessions/{sid}")).json()["commit_count"] == 0
