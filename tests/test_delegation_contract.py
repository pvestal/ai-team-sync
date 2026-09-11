"""Delegation as a first-class ATS object.

The contract, in one line: DELEGATION IS NOT HANDOFF. If Codex owns a task and
delegates a bounded subproblem to Claude, Codex still owns the task. Ownership
moves only by an explicit handoff, never as a side effect of a child finishing.

The second rule is that a mode is a SAFETY PROPERTY, not an audit label. If
READ_ONLY only decorated a record, it would be a note attached to a worker that
can still edit six files and fire the GPU.
"""

from __future__ import annotations

import pytest

from ai_team_sync.delegation import (IMPLEMENT, READ_ONLY, VERIFY,
                                     effective_authority, prohibitions_for)
from ai_team_sync.workers import registry


def test_read_only_forbids_every_mutating_act():
    forbidden = prohibitions_for(READ_ONLY)

    for act in ("file_write", "git_commit", "service_restart", "gpu_submit",
                "task_state_mutation", "recursive_delegation"):
        assert act in forbidden, f"READ_ONLY must forbid {act}"


def test_implement_allows_scoped_writes_but_never_closing_the_parent_task():
    forbidden = prohibitions_for(IMPLEMENT)

    assert "file_write" not in forbidden
    assert "git_commit" not in forbidden
    assert "parent_task_close" in forbidden
    assert "recursive_delegation" in forbidden


def test_verify_may_run_tests_but_not_edit_or_commit():
    forbidden = prohibitions_for(VERIFY)

    assert "file_write" in forbidden
    assert "git_commit" in forbidden
    assert "test_execution" not in forbidden


def test_mode_narrows_a_capable_worker():
    claude = registry().resolve("claude-code")
    assert claude.may_claim_scope and claude.may_commit

    auth = effective_authority(claude, READ_ONLY)

    assert auth.edit == "none"
    assert auth.commit is False
    assert auth.task_close == "no"


def test_mode_can_never_grant_what_the_worker_lacks():
    """A delegation is an intersection, not a promotion.

    Handing IMPLEMENT to a read-only local worker must not turn it into an
    editor — otherwise delegation becomes a privilege-escalation path around
    the registry.
    """
    local = registry().resolve("local:qwen3-30b")

    auth = effective_authority(local, IMPLEMENT)

    assert auth.edit == "none"
    assert auth.commit is False


def test_implement_keeps_the_editing_it_was_granted():
    claude = registry().resolve("claude-code")

    auth = effective_authority(claude, IMPLEMENT)

    assert auth.edit == "claimed_scope"
    assert auth.commit is True
    assert auth.task_close == "no", "a child never closes the parent's task"


async def _parent(client, **over):
    body = {"developer": "patrick", "agent": "codex", "scope": ["src/**"],
            "description": "own #2654", "repo_root": "/opt/anime-studio",
            "auto_lock": True}
    body.update(over)
    resp = await client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _delegate(client, parent_id, **over):
    body = {
        "parent_session_id": parent_id,
        "parent_task": "2654",
        "delegated_worker": "claude-code",
        "mode": READ_ONLY,
        "repo_root": "/opt/anime-studio",
        "scope": ["packages/scene_generation/**"],
        "objective": "trace every code path that binds identity for a pair keyframe",
        "acceptance": "decision points named with file:line, no edits, no renders",
        "lease_minutes": 30,
    }
    body.update(over)
    return await client.post("/api/delegations", json=body)


@pytest.mark.asyncio
async def test_a_delegation_records_the_whole_contract(client):
    parent = await _parent(client)

    resp = await _delegate(client, parent["id"])

    assert resp.status_code == 201, resp.text
    d = resp.json()
    assert d["parent_task"] == "2654"
    assert d["delegating_worker"] == "codex"
    assert d["delegated_worker"] == "claude-code"
    assert d["mode"] == READ_ONLY
    assert d["scope"] == ["packages/scene_generation/**"]
    assert d["acceptance"]
    assert "file_write" in d["prohibitions"]
    assert d["state"] == "open"
    assert d["lease_expires_at"]
    assert d["parent_owner_session_id"] == parent["id"]


@pytest.mark.asyncio
async def test_a_delegation_without_acceptance_criteria_is_refused(client):
    parent = await _parent(client)

    resp = await _delegate(client, parent["id"], acceptance="")

    assert resp.status_code == 422
    assert "acceptance" in resp.text


@pytest.mark.asyncio
async def test_the_child_cannot_claim_an_edit_scope_under_read_only(client):
    parent = await _parent(client)
    d = (await _delegate(client, parent["id"])).json()

    child = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:child1",
        "scope": ["packages/scene_generation/**"],
        "description": "delegated investigation",
        "repo_root": "/opt/anime-studio",
        "delegation_id": d["id"],
        "auto_lock": True,
    })

    assert child.status_code == 403
    assert child.json()["detail"]["error"] == "delegation_authority"


@pytest.mark.asyncio
async def test_the_child_registers_unscoped_and_is_attached_to_its_delegation(client):
    parent = await _parent(client)
    d = (await _delegate(client, parent["id"])).json()

    child = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:child1", "scope": [],
        "description": "delegated investigation", "repo_root": "/opt/anime-studio",
        "delegation_id": d["id"],
    })
    assert child.status_code == 201

    after = (await client.get(f"/api/delegations/{d['id']}")).json()
    assert after["child_session_id"] == child.json()["id"]


@pytest.mark.asyncio
async def test_returning_a_child_leaves_the_parent_owning_the_task(client):
    parent = await _parent(client)
    d = (await _delegate(client, parent["id"])).json()

    ret = await client.post(f"/api/delegations/{d['id']}/return", json={
        "result_summary": "identity binds in three places",
        "evidence": {"citations": ["packages/scene_generation/composite_image.py:214"]},
    })
    assert ret.status_code == 200
    assert ret.json()["state"] == "returned"

    parent_after = (await client.get(f"/api/sessions/{parent['id']}")).json()
    assert parent_after["status"] == "active", "the parent still owns the work"
    assert parent_after["lock_count"] == parent["lock_count"], "and still holds its locks"


@pytest.mark.asyncio
async def test_a_parent_cannot_be_completed_while_a_child_delegation_is_open(client):
    parent = await _parent(client)
    await _delegate(client, parent["id"])

    done = await client.patch(f"/api/sessions/{parent['id']}",
                              json={"status": "completed", "summary": "done"})

    assert done.status_code == 409
    assert done.json()["detail"]["error"] == "open_delegations"


@pytest.mark.asyncio
async def test_recursive_delegation_is_refused_at_depth_one(client):
    parent = await _parent(client)
    d = (await _delegate(client, parent["id"])).json()
    child = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:child1", "scope": [],
        "description": "delegated investigation", "repo_root": "/opt/anime-studio",
        "delegation_id": d["id"],
    })

    again = await _delegate(client, child.json()["id"], delegated_worker="codex")

    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "recursive_delegation"


@pytest.mark.asyncio
async def test_an_expired_lease_stops_accepting_evidence(client):
    parent = await _parent(client)
    d = (await _delegate(client, parent["id"], lease_minutes=0)).json()

    ret = await client.post(f"/api/delegations/{d['id']}/return", json={
        "result_summary": "too late", "evidence": {},
    })

    assert ret.status_code == 409
    assert ret.json()["detail"]["error"] == "lease_expired"
