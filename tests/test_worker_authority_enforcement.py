"""Authority is enforced by the SERVER, so it binds every worker equally.

Claude Code is guarded before the edit by a PreToolUse hook. Codex has no hook
mechanism and a local worker has no client, so the same rule expressed
client-side would bind exactly one of the three. These tests are the proof that
the refusal happens where all of them meet it.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_a_read_only_worker_cannot_claim_an_edit_scope(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "agent": "local:qwen3-30b",
        "scope": ["src/**"],
        "description": "triage the executor failures",
        "auto_lock": True,
    })

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "worker_authority"
    assert "local" in detail["message"]
    assert detail["worker"]["authority"]["edit"] == "none"


@pytest.mark.asyncio
async def test_a_read_only_worker_still_registers_so_its_work_is_visible(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick",
        "agent": "local:qwen3-30b",
        "scope": [],
        "description": "cluster the last 40 executor failures",
    })

    assert resp.status_code == 201, "read-only is not invisible; it claims nothing"
    assert resp.json()["agent"] == "local:qwen3-30b"


@pytest.mark.asyncio
async def test_concurrency_is_capped_per_worker_class(client):
    first = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "local:qwen3-30b",
        "scope": [], "description": "triage batch 1",
    })
    assert first.status_code == 201

    second = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "local:qwen3-30b",
        "scope": [], "description": "triage batch 2",
    })

    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "worker_concurrency"
    assert detail["limit"] == 1


@pytest.mark.asyncio
async def test_an_uncapped_worker_runs_many_sessions(client):
    for i in range(3):
        resp = await client.post("/api/sessions", json={
            "developer": "patrick", "agent": f"claude-code:cid{i}",
            "scope": [], "description": f"session {i}",
        })
        assert resp.status_code == 201, "the operator runs several Claude sessions at once"


@pytest.mark.asyncio
async def test_a_completed_session_does_not_count_against_the_cap(client):
    first = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "local:qwen3-30b",
        "scope": [], "description": "triage batch 1",
    })
    sid = first.json()["id"]
    done = await client.patch(f"/api/sessions/{sid}", json={
        "status": "completed", "summary": "clustered"})
    assert done.status_code == 200

    again = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "local:qwen3-30b",
        "scope": [], "description": "triage batch 2",
    })
    assert again.status_code == 201


@pytest.mark.asyncio
async def test_an_editing_worker_claims_scope_as_before(client):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "codex",
        "scope": ["docs/**"], "description": "bounded doc edit", "auto_lock": True,
    })

    assert resp.status_code == 201
    assert resp.json()["scope"] == ["docs/**"]


@pytest.mark.asyncio
async def test_the_registry_is_discoverable_so_a_worker_can_ask_what_it_may_do(client):
    listing = await client.get("/api/workers")
    assert listing.status_code == 200
    names = [w["worker"] for w in listing.json()]
    assert {"claude-code", "codex", "local", "default"} <= set(names)

    one = await client.get("/api/workers/local:qwen3-30b")
    assert one.status_code == 200
    body = one.json()
    assert body["worker"] == "local"
    assert body["authority"]["edit"] == "none"
    assert "failure_cluster" in body["capabilities"]


@pytest.mark.asyncio
async def test_an_unregistered_worker_cannot_claim_scope(client):
    """Unclassified identity can read, but cannot claim an edit scope."""
    one = await client.get("/api/workers/some-new-thing")
    assert one.status_code == 200
    assert one.json()["worker"] == "restricted"

    claim = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "some-new-thing",
        "scope": ["src/**"], "description": "unregistered worker claiming scope",
        "auto_lock": True,
    })
    assert claim.status_code == 403


@pytest.mark.asyncio
async def test_a_declared_read_only_worker_is_refused(client):
    """Declared read-only authority remains unchanged."""
    claim = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "local:gpt-oss-20b",
        "scope": ["src/**"], "description": "local worker claiming scope",
        "auto_lock": True,
    })
    assert claim.status_code == 403
