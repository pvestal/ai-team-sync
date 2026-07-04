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

async def _make_request(client) -> str:
    """Two sessions, one lock, one override request. Returns the request id."""
    owner = (await client.post("/api/sessions", json={
        "developer": "owner-dev", "scope": ["pkg/**"],
        "description": "holds the lock", "auto_lock": True,
    })).json()
    assert owner.get("id"), owner
    requester = (await client.post("/api/sessions", json={
        "developer": "req-dev", "auto_lock": False,
        "description": "wants in",
    })).json()
    resp = await client.post("/api/override-requests", json={
        "requester_session_id": requester["id"],
        "conflicting_pattern": "pkg/**",
        "justification": "small scoped change",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_get_by_prefix(client):
    rid = await _make_request(client)
    resp = await client.get(f"/api/override-requests/{rid[:8]}")
    assert resp.status_code == 200
    assert resp.json()["id"] == rid


@pytest.mark.asyncio
async def test_get_by_truncated_display_form(client):
    # The exact string agents used to paste: 8 chars + '...'
    rid = await _make_request(client)
    resp = await client.get(f"/api/override-requests/{rid[:8]}...")
    assert resp.status_code == 200
    assert resp.json()["id"] == rid


@pytest.mark.asyncio
async def test_short_prefix_404s(client):
    rid = await _make_request(client)
    resp = await client.get(f"/api/override-requests/{rid[:4]}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_respond_by_prefix(client):
    rid = await _make_request(client)
    resp = await client.post(
        f"/api/override-requests/{rid[:8]}/respond",
        json={"approved": True, "message": "go ahead"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == rid
    assert body["status"] == "approved"
