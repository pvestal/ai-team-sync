"""A message belongs to two exact ATS sessions and needs a receipt."""

import pytest
from ai_team_sync.hooks.override_inbox import format_message_inbox
from ai_team_sync.mcp.server import format_message_nudge


async def _session(client, agent):
    response = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": [],
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
    recipient, _ = await _session(client, "claude-code:two")
    response = await client.patch(f"/api/sessions/{recipient}", json={
        "status": "completed", "summary": "done"})
    assert response.status_code == 200, response.text
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": recipient,
        "body": "hello"}, headers={"X-ATS-Approval-Token": token})
    assert sent.status_code == 409


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
