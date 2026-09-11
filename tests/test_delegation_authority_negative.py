"""What must be REFUSED. Every case uses throwaway rows.

The happy path was proven live. These hold the other half: that a mode is a
safety property rather than a label, that ownership cannot be taken by asking,
and that an explicit id is not the same thing as authority over what it names.
"""

from __future__ import annotations

import pytest

from ai_team_sync.delegation import IMPLEMENT, READ_ONLY, effective_authority
from ai_team_sync.mcp.server import mutation_refusal
from ai_team_sync.workers import registry


async def _session(client, agent, scope, desc, **extra):
    body = {"developer": "patrick", "agent": agent, "scope": scope,
            "description": desc, "repo_root": "/opt/anime-studio", "auto_lock": True}
    body.update(extra)
    r = await client.post("/api/sessions", json=body)
    return r


async def _delegation(client, parent_id, mode=READ_ONLY):
    r = await client.post("/api/delegations", json={
        "parent_session_id": parent_id, "delegated_worker": "claude-code",
        "mode": mode, "repo_root": "/opt/anime-studio", "scope": ["src/**"],
        "objective": "investigate", "acceptance": "cites file:line"})
    assert r.status_code == 201, r.text
    return r.json()


async def _child(client, delegation_id, scope=None):
    return await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:delegate",
        "scope": scope or [], "description": "delegated child",
        "repo_root": "/opt/anime-studio", "delegation_id": delegation_id})


# 1 --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_read_only_child_cannot_escalate_itself_to_implement(client):
    parent = (await _session(client, "codex", ["src/**"], "owner")).json()
    d = await _delegation(client, parent["id"], READ_ONLY)
    child = await _child(client, d["id"])
    assert child.status_code == 201

    # (a) it cannot take the edit scope its mode denies
    grab = await _child(client, d["id"], scope=["src/**"])
    assert grab.status_code == 403
    assert grab.json()["detail"]["error"] == "delegation_authority"

    # (b) it cannot hand ITSELF a wider delegation
    escalate = await client.post("/api/delegations", json={
        "parent_session_id": child.json()["id"], "delegated_worker": "claude-code",
        "mode": IMPLEMENT, "objective": "let me edit", "acceptance": "x",
        "repo_root": "/opt/anime-studio", "scope": ["src/**"]})
    assert escalate.status_code == 409
    assert escalate.json()["detail"]["error"] == "recursive_delegation"


# 2 --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unrelated_session_cannot_reconcile_someone_elses_delegation(client):
    parent = (await _session(client, "codex", ["src/a/**"], "owner")).json()
    stranger = (await _session(client, "claude-code:zzzz", ["src/b/**"], "unrelated")).json()
    d = await _delegation(client, parent["id"])

    theft = await client.post(f"/api/delegations/{d['id']}/close", json={
        "state": "closed", "verdict": "not mine to give", "actor_session_id": stranger["id"]})

    assert theft.status_code == 403
    detail = theft.json()["detail"]
    assert detail["error"] == "not_the_owner"
    assert detail["parent_owner_session_id"] == parent["id"]

    still = (await client.get(f"/api/delegations/{d['id']}")).json()
    assert still["state"] == "open", "a refused reconcile changes nothing"


@pytest.mark.asyncio
async def test_reconciling_without_identifying_yourself_is_refused(client):
    parent = (await _session(client, "codex", ["src/a/**"], "owner")).json()
    d = await _delegation(client, parent["id"])

    anon = await client.post(f"/api/delegations/{d['id']}/close",
                             json={"state": "closed", "verdict": "who am i"})

    assert anon.status_code == 403
    assert anon.json()["detail"]["error"] == "no_actor"


# 3 --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_live_lock_is_not_reapable_by_a_stranger(client):
    holder = (await _session(client, "codex", ["src/held/**"], "holding")).json()
    stranger = (await _session(client, "claude-code:zzzz", ["src/other/**"], "unrelated")).json()
    lock_id = [l for l in (await client.get("/api/locks")).json()
               if l["session_id"] == holder["id"]][0]["id"]

    theft = await client.delete(f"/api/locks/{lock_id}",
                                params={"actor_session_id": stranger["id"]})

    assert theft.status_code == 403
    assert theft.json()["detail"]["error"] == "lock_not_yours"
    assert any(l["id"] == lock_id for l in (await client.get("/api/locks")).json())


@pytest.mark.asyncio
async def test_a_lock_held_by_a_ghost_session_is_still_reapable(client, monkeypatch):
    """The reap path must survive the new rule, or ghost locks become permanent.

    Completing a session already releases its locks, so the lock that needs
    reaping belongs to a session that still LOOKS active and has gone silent.
    """
    from ai_team_sync.config import settings
    monkeypatch.setattr(settings, "session_heartbeat_timeout_minutes", 0)

    ghost = (await _session(client, "codex", ["src/ghost/**"], "went silent")).json()
    lock_id = [l for l in (await client.get("/api/locks")).json()
               if l["session_id"] == ghost["id"]][0]["id"]

    reap = await client.delete(f"/api/locks/{lock_id}")

    assert reap.status_code == 204, "a ghost's lane must still be reclaimable"


@pytest.mark.asyncio
async def test_only_the_addressed_owner_answers_an_override_request(client):
    holder = (await _session(client, "codex", ["src/held/**"], "holding")).json()
    asker = (await _session(client, "claude-code:aaaa", ["src/mine/**"], "wants in")).json()
    stranger = (await _session(client, "claude-code:zzzz", ["src/other/**"], "unrelated")).json()
    req = await client.post("/api/override-requests", json={
        "requester_session_id": asker["id"],
        "conflicting_pattern": "src/held/**",
        "justification": "need to edit"})
    assert req.status_code in (200, 201), req.text
    assert req.json()["owner_session_id"] == holder["id"]
    rid = req.json()["id"]

    theft = await client.post(f"/api/override-requests/{rid}/respond", json={
        "approved": True, "message": "sure", "actor_session_id": stranger["id"]})

    assert theft.status_code == 403
    assert theft.json()["detail"]["error"] == "not_the_lock_owner"


# 4 --------------------------------------------------------------------------

def test_the_shared_pointer_alone_cannot_authorize_a_mutation():
    row = {"id": "sess-X", "agent": "codex", "status": "active"}

    assert mutation_refusal("sess-X", "global", row, "codex") is not None
    assert mutation_refusal(None, "none", None, "codex") is not None


# 5 --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_read_only_child_can_still_do_what_read_only_permits(client):
    parent = (await _session(client, "codex", ["src/**"], "owner")).json()
    d = await _delegation(client, parent["id"])
    child = await _child(client, d["id"])
    assert child.status_code == 201, "registering unscoped is allowed"

    auth = await client.get(f"/api/authority/{child.json()['id']}")
    assert auth.status_code == 200
    assert auth.json()["delegation"]["mode"] == READ_ONLY

    ret = await client.post(f"/api/delegations/{d['id']}/return", json={
        "result_summary": "three binding points", "evidence": {"citations": ["a.py:1"]},
        "actor_session_id": child.json()["id"]})
    assert ret.status_code == 200, "returning evidence is the child's whole job"
    assert ret.json()["state"] == "returned"


@pytest.mark.asyncio
async def test_a_stranger_cannot_submit_a_result_for_someone_elses_child(client):
    parent = (await _session(client, "codex", ["src/**"], "owner")).json()
    stranger = (await _session(client, "claude-code:zzzz", [], "unrelated")).json()
    d = await _delegation(client, parent["id"])
    await _child(client, d["id"])

    theft = await client.post(f"/api/delegations/{d['id']}/return", json={
        "result_summary": "made up", "evidence": {}, "actor_session_id": stranger["id"]})

    assert theft.status_code == 403
    assert theft.json()["detail"]["error"] == "not_the_child"


# 6 --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_base_edit_authority_does_not_survive_delegation_narrowing(client):
    """claude-code may edit and commit anywhere. Under READ_ONLY it may not."""
    base = registry().resolve("claude-code")
    assert base.may_claim_scope and base.may_commit

    parent = (await _session(client, "codex", ["src/**"], "owner")).json()
    d = await _delegation(client, parent["id"], READ_ONLY)
    child = await _child(client, d["id"])

    reported = (await client.get(f"/api/authority/{child.json()['id']}")).json()

    assert reported["base_authority"]["edit"] == "claimed_scope"
    assert reported["effective_authority"]["edit"] == "none"
    assert reported["effective_authority"]["commit"] is False
    assert reported["narrowed"] is True
    assert effective_authority(base, READ_ONLY).edit == "none"
