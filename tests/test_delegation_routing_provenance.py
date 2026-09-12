"""The SERVER refuses to record a delegation as work a worker did not do.

The launcher-side rules live in test_launch_spec_worker_identity.py. This file
covers the half a pure unit test cannot: a caller that spawns the wrong binary,
or reports nothing at all, must not end up with a stored row asserting that the
requested worker performed the work.

The rule is re-derived server-side on purpose. The parent reports what it
resolved, and the server checks that report against the registry itself, so a
record can claim Codex only when a Codex binary is what got resolved. Trusting
the caller's own pairing would make the provenance exactly as reliable as the
bug it replaces.
"""

from __future__ import annotations

import pytest

CLAUDE = "/usr/local/bin/claude"
CODEX = "/usr/bin/codex"


async def _parent(client, agent="claude-code:test"):
    r = await client.post("/api/sessions", json={
        "developer": "tester", "agent": agent, "scope": [],
        "description": "parent for a routing test", "auto_lock": False})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _delegate(client, parent_id, **over):
    body = {
        "parent_session_id": parent_id,
        "delegated_worker": "codex",
        "mode": "READ_ONLY",
        "objective": "inspect one function",
        "acceptance": "a file:line citation",
        "resolved_binary": CODEX,
        "launch_spec_version": "1",
    }
    body.update(over)
    return await client.post("/api/delegations", json=body)


@pytest.mark.asyncio
async def test_matching_worker_and_binary_is_recorded_with_both(client):
    parent = await _parent(client)
    r = await _delegate(client, parent)
    assert r.status_code == 201, r.text

    d = r.json()
    assert d["requested_worker"] == "codex"
    assert d["resolved_binary"] == CODEX
    assert d["launch_spec_version"] == "1"
    # The legacy field keeps working for existing readers.
    assert d["delegated_worker"] == "codex"


@pytest.mark.asyncio
async def test_requested_codex_but_claude_ran_is_refused(client):
    """The exact 2026-09-12 defect, as a server-side test.

    Delegations 82fb4676 and 5c04aa74 requested Codex, ran `claude -p`, and
    both closed as satisfied Codex work.
    """
    parent = await _parent(client)
    r = await _delegate(client, parent, resolved_binary=CLAUDE)

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "routing_failure"
    assert detail["requested_worker"] == "codex"
    assert detail["resolved_binary"] == CLAUDE


@pytest.mark.asyncio
async def test_a_refused_routing_stores_no_delegation_row(client):
    """A routing failure must leave nothing behind to reconcile later."""
    parent = await _parent(client)
    await _delegate(client, parent, resolved_binary=CLAUDE)

    listed = await client.get(f"/api/delegations?parent_session_id={parent}")
    assert listed.json() == []


@pytest.mark.asyncio
async def test_claude_delegation_still_works_unchanged(client):
    parent = await _parent(client)
    r = await _delegate(client, parent, delegated_worker="claude-code",
                        resolved_binary=CLAUDE)
    assert r.status_code == 201, r.text
    assert r.json()["resolved_binary"] == CLAUDE


@pytest.mark.asyncio
async def test_a_worker_with_no_launcher_cannot_claim_a_binary(client):
    """'local' is an authority class with no CLI, so no binary can evidence it."""
    parent = await _parent(client)
    r = await _delegate(client, parent, delegated_worker="local",
                        resolved_binary="/usr/bin/ollama")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "routing_failure"


@pytest.mark.asyncio
async def test_legacy_caller_without_a_resolved_binary_records_no_claim(client):
    """Back-compat: an older client that reports nothing still works, but its
    row must not imply the requested worker ran. Empty reads as 'unevidenced',
    which is the honest value for every historical row."""
    parent = await _parent(client)
    r = await _delegate(client, parent, resolved_binary="", launch_spec_version="")

    assert r.status_code == 201, r.text
    assert r.json()["resolved_binary"] is None
    assert r.json()["launch_spec_version"] is None
