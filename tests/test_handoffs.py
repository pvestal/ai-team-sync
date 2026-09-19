"""Ticket handoffs and deferred messages go to the first later claimant."""

import pytest
from sqlalchemy import update

from ai_team_sync.models import Session


async def _start(client, agent, ticket_id):
    response = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": [],
        "repo_root": "/repo", "ticket_id": ticket_id})
    assert response.status_code == 201, response.text
    return response.json(), response.headers["X-ATS-Approval-Token"]


@pytest.mark.asyncio
async def test_structured_handoff_reaches_first_next_ticket_session(client):
    source, source_token = await _start(client, "codex:source", 2907)
    completed = await client.patch(f"/api/sessions/{source['id']}", json={
        "status": "completed", "summary": "Blocked on delivery check",
        "handoff": {"verdict": "API ready", "blockers": ["old MCP process"],
                    "next_steps": ["restart client"], "artifacts": ["PR 5"]}},
        headers={"X-ATS-Approval-Token": source_token})
    assert completed.status_code == 200, completed.text
    duplicate = await client.patch(f"/api/sessions/{source['id']}", json={
        "status": "completed", "handoff": {"verdict": "duplicate"}},
        headers={"X-ATS-Approval-Token": source_token})
    assert duplicate.status_code == 409
    first, token = await _start(client, "claude-code:first", 2907)
    second, second_token = await _start(client, "claude-code:second", 2907)
    inbox = await client.get(f"/api/sessions/{first['id']}/messages",
                             headers={"X-ATS-Approval-Token": token})
    rows = [row for row in inbox.json() if row["kind"] == "handoff"]
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == 2907
    assert "old MCP process" in rows[0]["body"]
    other = await client.get(f"/api/sessions/{second['id']}/messages",
                             headers={"X-ATS-Approval-Token": second_token})
    assert other.json() == []


@pytest.mark.asyncio
async def test_handoff_remains_after_recipient_reap_and_resurrection(client, db_session):
    source, source_token = await _start(client, "codex:source", 2907)
    closed = await client.patch(f"/api/sessions/{source['id']}", json={
        "status": "completed", "summary": "ready",
        "handoff": {"verdict": "Continue", "next_steps": ["review"]}},
        headers={"X-ATS-Approval-Token": source_token})
    assert closed.status_code == 200, closed.text
    recipient, recipient_token = await _start(client, "claude-code:recipient", 2907)
    await db_session.execute(update(Session).where(Session.id == recipient["id"]).values(
        status="completed", auto_completed=True))
    await db_session.commit()
    revived = await client.post(f"/api/sessions/{recipient['id']}/heartbeat")
    assert revived.status_code == 200, revived.text
    assert revived.json()["status"] == "active"
    inbox = await client.get(f"/api/sessions/{recipient['id']}/messages",
                             headers={"X-ATS-Approval-Token": recipient_token})
    assert len(inbox.json()) == 1
    receipt = await client.post(
        f"/api/messages/{inbox.json()[0]['id']}/acknowledge",
        json={"recipient_session_id": recipient["id"]},
        headers={"X-ATS-Approval-Token": recipient_token})
    assert receipt.status_code == 200
    sender_status = await client.get(
        f"/api/messages/{inbox.json()[0]['id']}",
        params={"sender_session_id": source["id"]},
        headers={"X-ATS-Approval-Token": source_token})
    assert sender_status.status_code == 200
    assert sender_status.json()["acknowledged_at"] is not None
    closed_again = await client.patch(f"/api/sessions/{recipient['id']}", json={
        "status": "completed", "summary": "reviewed",
        "handoff": {"verdict": "Ship", "artifacts": ["review notes"]}},
        headers={"X-ATS-Approval-Token": recipient_token})
    assert closed_again.status_code == 200, closed_again.text
    third, third_token = await _start(client, "codex:third", 2907)
    last_inbox = await client.get(f"/api/sessions/{third['id']}/messages",
                                  headers={"X-ATS-Approval-Token": third_token})
    assert len([m for m in last_inbox.json() if m["kind"] == "handoff"]) == 1
    chain = await client.get("/api/tickets/2907/handoffs")
    assert chain.json()[0]["recipient_session_id"] == recipient["id"]
    assert chain.json()[0]["acknowledged_at"] is not None
    assert chain.json()[1]["recipient_session_id"] == third["id"]


@pytest.mark.asyncio
async def test_next_session_message_waits_for_later_ticket_claimant(client):
    sender, sender_token = await _start(client, "codex:sender", 2907)
    queued = await client.post("/api/messages", json={
        "sender_session_id": sender["id"], "ticket_id": 2907,
        "body": "Read the handoff first"},
        headers={"X-ATS-Approval-Token": sender_token})
    assert queued.status_code == 201, queued.text
    assert queued.json()["recipient_session_id"] is None
    unrelated, unrelated_token = await _start(client, "claude-code:wrong", 2622)
    assert (await client.get(f"/api/sessions/{unrelated['id']}/messages",
                             headers={"X-ATS-Approval-Token": unrelated_token})).json() == []
    recipient, recipient_token = await _start(client, "claude-code:right", 2907)
    inbox = await client.get(f"/api/sessions/{recipient['id']}/messages",
                             headers={"X-ATS-Approval-Token": recipient_token})
    assert [row["id"] for row in inbox.json()] == [queued.json()["id"]]
    status = await client.get(f"/api/messages/{queued.json()['id']}", params={
        "sender_session_id": sender["id"]},
        headers={"X-ATS-Approval-Token": sender_token})
    assert status.json()["recipient_session_id"] == recipient["id"]


@pytest.mark.asyncio
async def test_ticket_peer_sees_session_start_and_completion(client):
    observer, observer_token = await _start(client, "claude-code:observer", 2907)
    worker, worker_token = await _start(client, "codex:worker", 2907)
    endpoint = f"/api/sessions/{observer['id']}/messages"
    headers = {"X-ATS-Approval-Token": observer_token}
    started = (await client.get(endpoint, headers=headers)).json()
    assert len(started) == 1 and started[0]["kind"] == "event"
    assert worker["id"] in started[0]["body"]
    response = await client.patch(f"/api/sessions/{worker['id']}", json={
        "status": "completed", "summary": "Found blocker"},
        headers={"X-ATS-Approval-Token": worker_token})
    assert response.status_code == 200
    events = (await client.get(endpoint, headers=headers)).json()
    assert len(events) == 2
    assert "Found blocker" in events[1]["body"]
