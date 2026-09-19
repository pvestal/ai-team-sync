"""ats-override-push-p01: piggyback nudge + prefix-matched respond.

Covers the two layers that stop the operator being the message bus between
agents: format_override_nudge (appended to every MCP tool response for the
lock HOLDER) and id-prefix resolution on the respond/get endpoints (truncated
'3745bd63...' displays used to 404 when pasted back).
"""

from __future__ import annotations

import pytest

from ai_team_sync.mcp.server import format_override_nudge, _OVERRIDE_NUDGE_SKIP


# ---------------------------------------------------------------------------
# format_override_nudge (pure)
# ---------------------------------------------------------------------------

def _req(**over):
    base = {
        "id": "aaaabbbb-cccc-dddd-eeee-ffff00001111",
        "owner_session_id": "owner-1",
        "requester_session_id": "req-1",
        "requester_developer": "pvestal",
        "conflicting_pattern": "packages/scene_generation/**",
        "status": "pending",
    }
    base.update(over)
    return base


def test_nudge_none_when_no_requests():
    assert format_override_nudge([], "owner-1") is None


def test_nudge_owner_side_only():
    # This session is the REQUESTER, not the owner — no nudge.
    assert format_override_nudge([_req()], "req-1") is None


def test_nudge_skips_non_pending():
    assert format_override_nudge([_req(status="expired")], "owner-1") is None


def test_nudge_full_id_and_pattern():
    note = format_override_nudge([_req()], "owner-1")
    assert note is not None
    assert "aaaabbbb-cccc-dddd-eeee-ffff00001111" in note  # full id, paste-ready
    assert "packages/scene_generation/**" in note
    assert "respond_to_request" in note


def test_nudge_skip_list_covers_inbox_tools():
    # The tools that already render the inbox must be excluded from piggyback.
    assert "check_pending_requests" in _OVERRIDE_NUDGE_SKIP
    assert "respond_to_request" in _OVERRIDE_NUDGE_SKIP


# ---------------------------------------------------------------------------
# id-prefix resolution on the HTTP API
# ---------------------------------------------------------------------------

async def _make_request(client) -> tuple[str, str]:
    """Two sessions, one lock, one override request and the owner's capability."""
    owner_resp = await client.post("/api/sessions", json={
        "agent": "default",
        "developer": "owner-dev", "scope": ["pkg/**"],
        "description": "holds the lock", "auto_lock": True,
    })
    owner = owner_resp.json()
    assert owner.get("id"), owner
    assert "approval_token_hash" not in owner
    requester = (await client.post("/api/sessions", json={
        "agent": "default",
        "developer": "req-dev", "auto_lock": False,
        "description": "wants in",
    })).json()
    resp = await client.post("/api/override-requests", json={
        "requester_session_id": requester["id"],
        "conflicting_pattern": "pkg/**",
        "justification": "small scoped change",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], owner_resp.headers["x-ats-approval-token"]


@pytest.mark.asyncio
async def test_get_by_prefix(client):
    rid, _ = await _make_request(client)
    resp = await client.get(f"/api/override-requests/{rid[:8]}")
    assert resp.status_code == 200
    assert resp.json()["id"] == rid


@pytest.mark.asyncio
async def test_get_by_truncated_display_form(client):
    # The exact string agents used to paste: 8 chars + '...'
    rid, _ = await _make_request(client)
    resp = await client.get(f"/api/override-requests/{rid[:8]}...")
    assert resp.status_code == 200
    assert resp.json()["id"] == rid


@pytest.mark.asyncio
async def test_short_prefix_404s(client):
    rid, _ = await _make_request(client)
    resp = await client.get(f"/api/override-requests/{rid[:4]}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_respond_by_prefix(client):
    rid, token = await _make_request(client)
    owner = (await client.get(f"/api/override-requests/{rid}")).json()["owner_session_id"]
    resp = await client.post(
        f"/api/override-requests/{rid[:8]}/respond",
        json={"approved": True, "message": "go ahead", "actor_session_id": owner},
        headers={"X-ATS-Approval-Token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == rid
    assert body["status"] == "approved"


@pytest.mark.asyncio
async def test_override_response_requires_the_owner_session(client):
    rid, token = await _make_request(client)
    target = f"/api/override-requests/{rid}/respond"
    missing = await client.post(target, json={"approved": True})
    assert missing.status_code == 422
    wrong = await client.post(target, json={
        "approved": True, "actor_session_id": "another-session"})
    assert wrong.status_code == 403
    owner = (await client.get(f"/api/override-requests/{rid}")).json()["owner_session_id"]
    no_token = await client.post(target, json={
        "approved": True, "actor_session_id": owner})
    assert no_token.status_code == 403
    forged = await client.post(target, json={
        "approved": True, "actor_session_id": owner},
        headers={"X-ATS-Approval-Token": "not-the-token"})
    assert forged.status_code == 403
    assert (await client.get(f"/api/override-requests/{rid}")).json()["status"] == "pending"


@pytest.mark.asyncio
async def test_approved_override_is_scoped_to_requester_and_enforced(client):
    rid, token = await _make_request(client)
    request = (await client.get(f"/api/override-requests/{rid}")).json()
    requester = request["requester_session_id"]
    blocked = await client.post("/api/locks", json={
        "session_id": requester, "pattern": "pkg/**", "mode": "exclusive"})
    assert blocked.status_code == 409

    response = await client.post(f"/api/override-requests/{rid}/respond", json={
        "approved": True, "actor_session_id": request["owner_session_id"]},
        headers={"X-ATS-Approval-Token": token})
    assert response.status_code == 200

    coverage = (await client.post("/api/locks/check", json={
        "paths": ["pkg/a.py"], "session_id": requester})).json()[0]
    assert any(m["override_granted"] for m in coverage["matches"])
    allowed = await client.post("/api/locks", json={
        "session_id": requester, "pattern": "pkg/**", "mode": "exclusive"})
    assert allowed.status_code == 201, allowed.text

    commit = (await client.post("/api/git/pre-commit-check", json={
        "staged_files": ["pkg/a.py"], "session_id": requester})).json()
    assert commit["blocking_locks"] == []
    assert commit["can_proceed"] is True
