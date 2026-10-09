"""POST /api/delegations/{id}/verify-child: proof a process IS a live delegated child.

The ATS-first prompt hook skips re-resolution for a delegated child, whose
packet already carries its context. Delegation and child ids are readable by
anyone (GET /api/delegations), so they cannot be the credential (review
fd3d2e5e, finding 1): the child's own approval token is. And 'open' is not
liveness, because expiry is only applied on /return (finding 2), so the lease
and the child session's status are checked here too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_team_sync.models import Delegation, Session

CODEX = "/usr/bin/codex"


async def _live_child(client):
    parent = await client.post("/api/sessions", json={
        "developer": "tester", "agent": "claude-code:parent", "scope": [],
        "description": "parent", "auto_lock": False})
    assert parent.status_code in (200, 201), parent.text
    d = await client.post("/api/delegations", json={
        "parent_session_id": parent.json()["id"], "delegated_worker": "codex",
        "mode": "VERIFY", "objective": "review", "acceptance": "PASS or BLOCK",
        "resolved_binary": CODEX, "launch_spec_version": "2"})
    assert d.status_code == 201, d.text
    child = await client.post("/api/sessions", json={
        "developer": "tester", "agent": "codex:delegate", "scope": [],
        "description": "child", "delegation_id": d.json()["id"]})
    assert child.status_code in (200, 201), child.text
    return d.json()["id"], child.json()["id"], child.headers["X-ATS-Approval-Token"]


async def _verify(client, delegation_id, child_id, token):
    return await client.post(f"/api/delegations/{delegation_id}/verify-child",
                             json={"child_session_id": child_id},
                             headers={"X-ATS-Approval-Token": token} if token else {})


async def test_the_live_child_with_its_own_token_is_confirmed(client):
    did, cid, token = await _live_child(client)

    r = await _verify(client, did, cid, token)

    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "VERIFY"


async def test_ids_alone_are_not_a_credential(client):
    did, cid, _token = await _live_child(client)

    assert (await _verify(client, did, cid, "")).status_code == 403
    assert (await _verify(client, did, cid, "guessed-token")).status_code == 403


async def test_another_sessions_token_does_not_prove_this_child(client):
    did, cid, _token = await _live_child(client)
    _did2, _cid2, other_token = await _live_child(client)

    assert (await _verify(client, did, cid, other_token)).status_code == 403


async def test_an_elapsed_lease_is_not_live_even_while_stored_open(client, db_session):
    did, cid, token = await _live_child(client)
    row = await db_session.get(Delegation, did)
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    r = await _verify(client, did, cid, token)

    assert r.status_code == 409
    assert "lease" in r.text


async def test_a_completed_child_session_is_not_live(client, db_session):
    did, cid, token = await _live_child(client)
    child = await db_session.get(Session, cid)
    child.status = "completed"
    await db_session.commit()

    r = await _verify(client, did, cid, token)

    assert r.status_code == 409


async def test_a_returned_delegation_is_not_live(client, db_session):
    did, cid, token = await _live_child(client)
    row = await db_session.get(Delegation, did)
    row.state = "returned"
    await db_session.commit()

    assert (await _verify(client, did, cid, token)).status_code == 409
