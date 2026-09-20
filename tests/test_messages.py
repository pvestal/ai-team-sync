"""A message belongs to two exact ATS sessions and needs a receipt."""

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.hooks.override_inbox import format_message_inbox
from ai_team_sync.mcp.server import format_message_nudge
from ai_team_sync.models import ScopeLock, Session


async def _session(client, agent, *, ticket_id=None):
    response = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": [],
        "ticket_id": ticket_id,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"], response.headers["X-ATS-Approval-Token"]


@pytest.mark.asyncio
async def test_message_requires_sender_capability_and_recipient_ack(client):
    sender, sender_token = await _session(client, "codex:one")
    recipient, recipient_token = await _session(client, "claude-code:two")
    body = {"sender_session_id": sender, "recipient_session_id": recipient,
            "body": "Please restart ATS MCP at your next safe pause."}
    assert (await client.post("/api/messages", json=body)).status_code == 403
    assert (await client.post("/api/messages", json=body, headers={
        "X-ATS-Approval-Token": recipient_token})).status_code == 403
    sent = await client.post("/api/messages", json=body, headers={
        "X-ATS-Approval-Token": sender_token})
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["sender_agent"] == "codex:one"
    assert message["recipient_agent"] == "claude-code:two"
    assert message["acknowledged_at"] is None

    endpoint = f"/api/sessions/{recipient}/messages"
    assert (await client.get(endpoint)).status_code == 403
    inbox = await client.get(endpoint, headers={"X-ATS-Approval-Token": recipient_token})
    assert [row["id"] for row in inbox.json()] == [message["id"]]
    ack_endpoint = f"/api/messages/{message['id']}/acknowledge"
    assert (await client.post(ack_endpoint, json={"recipient_session_id": sender},
                              headers={"X-ATS-Approval-Token": sender_token})).status_code == 404
    ack = await client.post(ack_endpoint, json={"recipient_session_id": recipient},
                            headers={"X-ATS-Approval-Token": recipient_token})
    assert ack.status_code == 200, ack.text
    assert ack.json()["acknowledged_at"] is not None
    assert (await client.get(endpoint, headers={
        "X-ATS-Approval-Token": recipient_token})).json() == []
    status = await client.get(f"/api/messages/{message['id']}", params={
        "sender_session_id": sender}, headers={"X-ATS-Approval-Token": sender_token})
    assert status.json()["acknowledged_at"] is not None


@pytest.mark.asyncio
async def test_completed_recipient_cannot_be_messaged(client):
    sender, token = await _session(client, "codex:one")
    recipient, recipient_token = await _session(client, "claude-code:two")
    response = await client.patch(f"/api/sessions/{recipient}", json={
        "status": "completed", "summary": "done"}, headers={
            "X-ATS-Approval-Token": recipient_token})
    assert response.status_code == 200, response.text
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": recipient,
        "body": "hello"}, headers={"X-ATS-Approval-Token": token})
    assert sent.status_code == 409


@pytest.mark.asyncio
async def test_unread_direct_message_can_be_readdressed_by_sender_after_turnover(client):
    sender, sender_token = await _session(client, "codex:sender", ticket_id=2907)
    old, old_token = await _session(client, "claude-code:old", ticket_id=2907)
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": old, "body": "keep this text",
    }, headers={"X-ATS-Approval-Token": sender_token})
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["id"]
    assert (await client.patch(f"/api/sessions/{old}", json={
        "status": "completed", "summary": "turnover",
    }, headers={"X-ATS-Approval-Token": old_token})).status_code == 200
    successor, successor_token = await _session(client, "claude-code:new", ticket_id=2907)
    unrelated, unrelated_token = await _session(client, "codex:unrelated", ticket_id=9999)

    endpoint = f"/api/messages/{message_id}/readdress"
    body = {"sender_session_id": sender, "recipient_session_id": successor}
    assert (await client.post(endpoint, json=body, headers={
        "X-ATS-Approval-Token": successor_token})).status_code == 403
    assert (await client.post(endpoint, json=body, headers={
        "X-ATS-Approval-Token": unrelated_token})).status_code == 403
    assert (await client.post(endpoint, json={
        "sender_session_id": sender, "recipient_session_id": unrelated,
    }, headers={"X-ATS-Approval-Token": sender_token})).status_code == 403
    assert (await client.get(f"/api/sessions/{unrelated}/messages", headers={
        "X-ATS-Approval-Token": unrelated_token})).json() == []
    moved = await client.post(endpoint, json=body, headers={
        "X-ATS-Approval-Token": sender_token})
    assert moved.status_code == 200, moved.text
    assert moved.json()["id"] == message_id
    assert moved.json()["original_recipient_session_id"] == old
    assert moved.json()["recipient_session_id"] == successor
    assert moved.json()["body"] == "keep this text"
    assert [event["recipient_session_id"] for event in moved.json()["delivery_history"]
            if event["action"] == "assigned"] == [old, successor]
    inbox = await client.get(f"/api/sessions/{successor}/messages", headers={
        "X-ATS-Approval-Token": successor_token})
    assert [row["id"] for row in inbox.json() if row["id"] == message_id] == [message_id]
    assert (await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": old}, headers={"X-ATS-Approval-Token": old_token})).status_code == 403
    first_ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": successor}, headers={"X-ATS-Approval-Token": successor_token})
    assert first_ack.status_code == 200, first_ack.text
    assert first_ack.json()["acknowledged_at"] is not None
    second_ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": successor}, headers={"X-ATS-Approval-Token": successor_token})
    assert second_ack.json()["acknowledged_at"] == first_ack.json()["acknowledged_at"]
    assert (await client.post(endpoint, json=body, headers={
        "X-ATS-Approval-Token": sender_token})).status_code == 409
    status = await client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers={"X-ATS-Approval-Token": sender_token})
    assert status.json()["acknowledged_at"] == first_ack.json()["acknowledged_at"]


@pytest.mark.asyncio
async def test_ticket_message_requeues_after_unread_recipient_ends(client):
    sender, sender_token = await _session(client, "codex:sender", ticket_id=2899)
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "ticket_id": 2899, "body": "durable handoff",
    }, headers={"X-ATS-Approval-Token": sender_token})
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["id"]
    assert (await client.patch(f"/api/sessions/{sender}", json={
        "status": "completed", "summary": "sender done",
    }, headers={"X-ATS-Approval-Token": sender_token})).status_code == 200
    first, first_token = await _session(client, "claude-code:first", ticket_id=2899)
    assert message_id in [row["id"] for row in (await client.get(
        f"/api/sessions/{first}/messages", headers={
            "X-ATS-Approval-Token": first_token})).json()]
    assert (await client.patch(f"/api/sessions/{first}", json={
        "status": "completed", "summary": "recipient unread",
    }, headers={"X-ATS-Approval-Token": first_token})).status_code == 200
    wrong, wrong_token = await _session(client, "claude-code:wrong", ticket_id=2907)
    assert message_id not in [row["id"] for row in (await client.get(
        f"/api/sessions/{wrong}/messages", headers={
            "X-ATS-Approval-Token": wrong_token})).json()]
    second, second_token = await _session(client, "claude-code:second", ticket_id=2899)
    inbox = await client.get(f"/api/sessions/{second}/messages", headers={
        "X-ATS-Approval-Token": second_token})
    assert message_id in [row["id"] for row in inbox.json()]
    assert (await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": wrong}, headers={
            "X-ATS-Approval-Token": wrong_token})).status_code == 404
    assert (await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": first}, headers={
            "X-ATS-Approval-Token": first_token})).status_code == 403
    ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": second}, headers={"X-ATS-Approval-Token": second_token})
    assert ack.status_code == 200, ack.text
    status = await client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers={"X-ATS-Approval-Token": sender_token})
    assert status.json()["acknowledged_at"] == ack.json()["acknowledged_at"]
    assert status.json()["original_recipient_session_id"] is None
    assert [event["recipient_session_id"] for event in status.json()["delivery_history"]
            if event["action"] == "assigned"] == [first, second]


@pytest.mark.asyncio
async def test_reaper_keeps_unread_ticket_with_revivable_owner(client, db_session):
    sender, sender_token = await _session(client, "codex:sender", ticket_id=2907)
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "ticket_id": 2907, "body": "survive reap",
    }, headers={"X-ATS-Approval-Token": sender_token})
    message_id = sent.json()["id"]
    old, old_token = await _session(client, "claude-code:old", ticket_id=2907)
    live, _ = await _session(client, "claude-code:live", ticket_id=2907)
    now = datetime.now(timezone.utc)
    old_row = await db_session.get(Session, old)
    old_row.started_at = now - timedelta(hours=1)
    old_row.last_heartbeat = now - timedelta(
        minutes=settings.session_heartbeat_timeout_minutes + 2)
    db_session.add_all([
        ScopeLock(session_id=old, pattern="old/**", mode="advisory",
                  created_at=now - timedelta(hours=1),
                  expires_at=now + timedelta(hours=1)),
        ScopeLock(session_id=live, pattern="live/**", mode="advisory",
                  expires_at=now + timedelta(hours=1)),
    ])
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await db_session.get(Session, old)).status == "completed"
    locks = (await db_session.execute(select(ScopeLock))).scalars().all()
    assert [lock.session_id for lock in locks] == [live]
    successor, successor_token = await _session(client, "claude-code:successor",
                                                ticket_id=2907)
    inbox = await client.get(f"/api/sessions/{successor}/messages", headers={
        "X-ATS-Approval-Token": successor_token})
    assert message_id not in [row["id"] for row in inbox.json()]
    assert (await client.post(f"/api/sessions/{old}/heartbeat")).status_code == 200
    old_inbox = await client.get(f"/api/sessions/{old}/messages", headers={
        "X-ATS-Approval-Token": old_token})
    assert message_id in [row["id"] for row in old_inbox.json()]
    assert [event["action"] for event in old_inbox.json()[0]["delivery_history"]] == [
        "assigned"]


def test_client_delivery_text_includes_sender_and_ack_instruction():
    rows = [{"id": "message-1", "sender_agent": "claude-code:abc",
             "sender_session_id": "session-1", "body": "Check the canary"}]
    assert "claude-code:abc" in format_message_inbox(rows)
    assert "acknowledge_message" in format_message_inbox(rows)
    assert "message-1" in format_message_nudge(rows)


@pytest.mark.asyncio
async def test_team_decisions_can_be_filtered_to_a_repo(client):
    for repo, title in [("/repo/a", "From Claude"), ("/repo/b", "Other repo")]:
        created = await client.post("/api/sessions", json={
            "developer": "pvestal", "agent": "claude-code", "scope": [],
            "repo_root": repo})
        sid = created.json()["id"]
        response = await client.post("/api/decisions", json={
            "session_id": sid, "title": title, "chosen": "ship",
            "reasoning": "verified", "files": []})
        assert response.status_code == 201, response.text
    team = await client.get("/api/decisions", params={"repo_root": "/repo/a", "limit": 10})
    assert [d["title"] for d in team.json()] == ["From Claude"]


@pytest.mark.asyncio
async def test_addressed_decision_reaches_recipient_inbox(client):
    sender, sender_token = await _session(client, "claude-code:sender")
    recipient, recipient_token = await _session(client, "codex:recipient")
    body = {"session_id": sender, "recipient_session_id": recipient,
            "title": "Canary gate", "chosen": "Preservation blocks",
            "reasoning": "review required", "files": []}
    assert (await client.post("/api/decisions", json=body)).status_code == 403
    created = await client.post("/api/decisions", json=body, headers={
        "X-ATS-Approval-Token": sender_token})
    assert created.status_code == 201, created.text
    assert created.json()["recipient_session_id"] == recipient
    inbox = await client.get(f"/api/sessions/{recipient}/messages",
                             headers={"X-ATS-Approval-Token": recipient_token})
    assert len(inbox.json()) == 1
    assert inbox.json()[0]["kind"] == "decision"
    assert "Preservation blocks" in inbox.json()[0]["body"]
