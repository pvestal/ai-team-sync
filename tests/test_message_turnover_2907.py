"""#2907 authorization and reversible ticket-mail ownership regressions."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from ai_team_sync import peer_identity
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import Session


async def new_session(client, agent, ticket=2907):
    response = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "ticket_id": ticket, "scope": [],
    })
    assert response.status_code == 201, response.text
    return response.json()["id"], response.headers["X-ATS-Approval-Token"]


def auth(token):
    return {"X-ATS-Approval-Token": token}


async def send_ticket(client, sender, token, ticket=2907):
    response = await client.post("/api/messages", json={
        "sender_session_id": sender, "ticket_id": ticket, "body": "one instruction",
    }, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def inbox(client, session, token):
    response = await client.get(f"/api/sessions/{session}/messages", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [False, True])
async def test_foreign_capability_cannot_complete_or_release_claimed_ticket(client, alias):
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    assert message_id in [m["id"] for m in await inbox(client, owner, owner_token)]
    foreign, foreign_token = await new_session(client, "codex:foreign", ticket=9999)
    url = f"/api/sessions/{owner}" + ("/complete" if alias else "")
    request = client.post if alias else client.patch
    body = {"status": "completed"}
    for headers in ({}, auth(foreign_token)):
        denied = await request(url, json=body, headers=headers)
        assert denied.status_code == 403, denied.text
        successor, successor_token = await new_session(client, "claude-code:too-early")
        assert message_id not in [m["id"] for m in await inbox(client, successor, successor_token)]
    assert message_id in [m["id"] for m in await inbox(client, owner, owner_token)]
    completed = await request(url, json=body, headers=auth(owner_token))
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    successor, successor_token = await new_session(client, "claude-code:successor")
    rows = await inbox(client, successor, successor_token)
    assert [m["id"] for m in rows if m["id"] == message_id] == [message_id]
    message = next(m for m in rows if m["id"] == message_id)
    assert message["original_recipient_session_id"] is None
    assert [e["recipient_session_id"] for e in message["delivery_history"]
            if e["action"] == "assigned"] == [owner, successor]
    assert message["delivery_history"][1]["reason"] == "recipient_completed"
    denied_ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": foreign}, headers=auth(foreign_token))
    assert denied_ack.status_code == 404
    ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": successor}, headers=auth(successor_token))
    assert ack.status_code == 200 and ack.json()["acknowledged_at"]
    receipt = await client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers=auth(sender_token))
    assert receipt.json()["acknowledged_at"] == ack.json()["acknowledged_at"]


@pytest.mark.asyncio
async def test_ticket_claim_and_peer_events_stay_with_creating_account(client, monkeypatch):
    peer = {"uid": 1001}
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: peer["uid"])
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    peer["uid"] = 1002
    foreign, foreign_token = await new_session(client, "claude-code:foreign")
    assert message_id not in [m["id"] for m in await inbox(client, foreign, foreign_token)]
    peer["uid"] = 1001
    owner, owner_token = await new_session(client, "claude-code:owner")
    assert message_id in [m["id"] for m in await inbox(client, owner, owner_token)]
    # A completion event is a direct copy to current peers in this account.
    assert (await client.patch(f"/api/sessions/{sender}", json={
        "status": "completed"}, headers=auth(sender_token))).status_code == 200
    peer["uid"] = 1002
    assert all(m["kind"] != "event" for m in await inbox(client, foreign, foreign_token))


@pytest.mark.asyncio
async def test_readdress_requires_original_sender_even_with_other_valid_capability(client):
    sender, sender_token = await new_session(client, "codex:sender")
    other, other_token = await new_session(client, "codex:other")
    old, old_token = await new_session(client, "claude-code:old")
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": old, "body": "direct",
    }, headers=auth(sender_token))
    message_id = sent.json()["id"]
    assert (await client.patch(f"/api/sessions/{old}", json={
        "status": "completed"}, headers=auth(old_token))).status_code == 200
    next_owner, next_token = await new_session(client, "claude-code:next")
    url = f"/api/messages/{message_id}/readdress"
    for actor, token in ((sender, other_token), (other, other_token)):
        denied = await client.post(url, json={
            "sender_session_id": actor, "recipient_session_id": next_owner,
        }, headers=auth(token))
        assert denied.status_code in (403, 404), denied.text
    assert message_id not in [m["id"] for m in await inbox(client, next_owner, next_token)]
    moved = await client.post(url, json={
        "sender_session_id": sender, "recipient_session_id": next_owner,
    }, headers=auth(sender_token))
    assert moved.status_code == 200, moved.text
    assert moved.json()["original_recipient_session_id"] == old
    assert moved.json()["sender_session_id"] == sender


@pytest.mark.asyncio
async def test_reap_then_late_heartbeat_preserves_only_original_mail_owner(client, db_session):
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    row = await db_session.get(Session, owner)
    row.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    row.last_heartbeat = datetime.now(timezone.utc) - timedelta(
        minutes=settings.session_heartbeat_timeout_minutes + 2)
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    successor, successor_token = await new_session(client, "claude-code:successor")
    assert message_id not in [m["id"] for m in await inbox(client, successor, successor_token)]
    resurrected = await client.post(f"/api/sessions/{owner}/heartbeat")
    assert resurrected.status_code == 200 and resurrected.json()["status"] == "active"
    assert message_id in [m["id"] for m in await inbox(client, owner, owner_token)]
    assert (await client.patch(f"/api/sessions/{owner}", json={
        "status": "completed"}, headers=auth(owner_token))).status_code == 200
    assert (await client.post(f"/api/sessions/{owner}/heartbeat")).status_code == 409
    assert message_id not in [m["id"] for m in await inbox(client, successor, successor_token)]
    later, later_token = await new_session(client, "claude-code:later")
    assert message_id in [m["id"] for m in await inbox(client, later, later_token)]


@pytest.mark.asyncio
async def test_successor_claim_before_late_heartbeat_blocks_resurrection(client, db_session):
    sender, sender_token = await new_session(client, "codex:sender")
    mid = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    row = await db_session.get(Session, owner)
    row.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    row.last_heartbeat = datetime.now(timezone.utc) - timedelta(
        minutes=settings.session_heartbeat_timeout_minutes + 2)
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    # Only the original capability holder can turn a reversible reap terminal.
    assert (await client.patch(f"/api/sessions/{owner}", json={
        "status": "completed"}, headers=auth(owner_token))).status_code == 200
    successor, successor_token = await new_session(client, "claude-code:successor")
    assert mid in [m["id"] for m in await inbox(client, successor, successor_token)]
    assert (await client.post(f"/api/sessions/{owner}/heartbeat")).status_code == 409
    assert mid in [m["id"] for m in await inbox(client, successor, successor_token)]
    assert (await client.get(f"/api/sessions/{owner}")).json()["status"] == "completed"


@pytest.mark.asyncio
async def test_explicit_completion_cannot_patch_reopen_after_successor_claim(client):
    sender, sender_token = await new_session(client, "codex:sender")
    mid = await send_ticket(client, sender, sender_token)
    old, old_token = await new_session(client, "claude-code:old")
    assert mid in [m["id"] for m in await inbox(client, old, old_token)]
    assert (await client.patch(f"/api/sessions/{old}", json={
        "status": "completed"}, headers=auth(old_token))).status_code == 200
    successor, successor_token = await new_session(client, "claude-code:successor")
    assert mid in [m["id"] for m in await inbox(client, successor, successor_token)]
    for status in ("active", "paused"):
        attempt = await client.patch(f"/api/sessions/{old}", json={
            "status": status}, headers=auth(old_token))
        assert attempt.status_code == 409, attempt.text
    assert (await client.post(f"/api/sessions/{old}/heartbeat")).status_code == 409
    assert (await client.get(f"/api/sessions/{old}/messages",
                             headers=auth(old_token))).status_code == 403
    ack = await client.post(f"/api/messages/{mid}/acknowledge", json={
        "recipient_session_id": successor}, headers=auth(successor_token))
    assert ack.status_code == 200
    status = await client.get(f"/api/messages/{mid}", params={
        "sender_session_id": sender}, headers=auth(sender_token))
    assert status.json()["acknowledged_at"] == ack.json()["acknowledged_at"]


@pytest.mark.asyncio
async def test_paused_session_retains_mail_and_locks_until_owner_resumes(client):
    sender, sender_token = await new_session(client, "codex:sender")
    mid = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    paused = await client.patch(f"/api/sessions/{owner}", json={
        "status": "paused"}, headers=auth(owner_token))
    assert paused.status_code == 200
    assert (await client.get(f"/api/sessions/{owner}/messages",
                             headers=auth(owner_token))).status_code == 403
    successor, successor_token = await new_session(client, "claude-code:successor")
    assert mid not in [m["id"] for m in await inbox(client, successor, successor_token)]
    assert (await client.patch(f"/api/sessions/{owner}", json={
        "status": "active"}, headers=auth(owner_token))).status_code == 200
    assert mid in [m["id"] for m in await inbox(client, owner, owner_token)]


@pytest.mark.asyncio
async def test_invalid_status_cannot_strand_ticket_mail(client):
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    for value in ("abandoned", "reaped", "", 123):
        invalid = await client.patch(f"/api/sessions/{owner}", json={
            "status": value}, headers=auth(owner_token))
        assert invalid.status_code == 422, invalid.text
    assert (await client.get(f"/api/sessions/{owner}")).json()["status"] == "active"
    assert message_id in [m["id"] for m in await inbox(client, owner, owner_token)]


@pytest.mark.asyncio
async def test_send_racing_owner_completion_keeps_direct_provenance(client):
    sender, sender_token = await new_session(client, "codex:sender")
    old, old_token = await new_session(client, "claude-code:old")
    send, close = await asyncio.gather(
        client.post("/api/messages", json={
            "sender_session_id": sender, "recipient_session_id": old,
            "body": "racing direct",
        }, headers=auth(sender_token)),
        client.patch(f"/api/sessions/{old}", json={"status": "completed"},
                     headers=auth(old_token)),
    )
    assert close.status_code == 200, close.text
    assert send.status_code in (201, 409), send.text
    if send.status_code == 201:
        message = send.json()
        assert message["sender_session_id"] == sender
        assert message["original_recipient_session_id"] == old
        successor, successor_token = await new_session(client, "claude-code:successor")
        moved = await client.post(f"/api/messages/{message['id']}/readdress", json={
            "sender_session_id": sender, "recipient_session_id": successor,
        }, headers=auth(sender_token))
        assert moved.status_code == 200, moved.text
        assert [m["id"] for m in await inbox(client, successor, successor_token)
                if m["id"] == message["id"]] == [message["id"]]


@pytest.mark.asyncio
async def test_send_racing_foreign_completion_does_not_end_recipient(client):
    sender, sender_token = await new_session(client, "codex:sender")
    recipient, recipient_token = await new_session(client, "claude-code:recipient")
    attacker, attacker_token = await new_session(client, "codex:attacker")
    send, close = await asyncio.gather(
        client.post("/api/messages", json={
            "sender_session_id": sender, "recipient_session_id": recipient,
            "body": "still owned",
        }, headers=auth(sender_token)),
        client.patch(f"/api/sessions/{recipient}", json={"status": "completed"},
                     headers=auth(attacker_token)),
    )
    assert close.status_code == 403
    assert send.status_code == 201, send.text
    assert (await client.get(f"/api/sessions/{recipient}")).json()["status"] == "active"
    assert send.json()["id"] in [m["id"] for m in await inbox(client, recipient, recipient_token)]
    assert send.json()["id"] not in [m["id"] for m in await inbox(client, attacker, attacker_token)]


@pytest.mark.asyncio
async def test_inbox_read_is_not_ack_when_completion_intervenes(client):
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    assert message_id in [m["id"] for m in await inbox(client, owner, owner_token)]
    assert (await client.patch(f"/api/sessions/{owner}", json={
        "status": "completed"}, headers=auth(owner_token))).status_code == 200
    old_ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": owner}, headers=auth(owner_token))
    assert old_ack.status_code == 403
    successor, successor_token = await new_session(client, "claude-code:next")
    assert message_id in [m["id"] for m in await inbox(client, successor, successor_token)]
    ack = await client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": successor}, headers=auth(successor_token))
    assert ack.status_code == 200
    status = await client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers=auth(sender_token))
    assert status.json()["acknowledged_at"] == ack.json()["acknowledged_at"]


@pytest.mark.asyncio
async def test_concurrent_ack_has_one_receipt_and_no_reclaim(client):
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    endpoint = f"/api/messages/{message_id}/acknowledge"
    replies = await asyncio.gather(*[
        client.post(endpoint, json={"recipient_session_id": owner},
                    headers=auth(owner_token)) for _ in range(2)])
    assert all(reply.status_code in (200, 409) for reply in replies)
    persisted = await client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers=auth(sender_token))
    receipt = persisted.json()["acknowledged_at"]
    assert receipt
    assert all(reply.json().get("acknowledged_at") in (None, receipt)
               for reply in replies if reply.status_code == 200)
    assert (await client.patch(f"/api/sessions/{owner}", json={
        "status": "completed"}, headers=auth(owner_token))).status_code == 200
    next_owner, next_token = await new_session(client, "claude-code:next")
    assert message_id not in [m["id"] for m in await inbox(client, next_owner, next_token)]


@pytest.mark.asyncio
async def test_ack_and_release_interleaving_has_one_outcome(client):
    sender, sender_token = await new_session(client, "codex:sender")
    message_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:owner")
    ack, close = await asyncio.gather(
        client.post(f"/api/messages/{message_id}/acknowledge", json={
            "recipient_session_id": owner}, headers=auth(owner_token)),
        client.patch(f"/api/sessions/{owner}", json={"status": "completed"},
                     headers=auth(owner_token)),
    )
    assert close.status_code == 200, close.text
    status = await client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers=auth(sender_token))
    row = status.json()
    successor, successor_token = await new_session(client, "claude-code:next")
    if row["acknowledged_at"]:
        assert row["recipient_session_id"] == owner
        assert message_id not in [m["id"] for m in await inbox(client, successor, successor_token)]
        assert ack.status_code in (200, 409)
    else:
        assert row["recipient_session_id"] == successor
        assert message_id in [m["id"] for m in await inbox(client, successor, successor_token)]
        assert ack.status_code in (403, 409)


@pytest.mark.asyncio
async def test_ack_and_readdress_are_mutually_exclusive(client):
    sender, sender_token = await new_session(client, "codex:sender")
    old, old_token = await new_session(client, "claude-code:old")
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": old, "body": "direct",
    }, headers=auth(sender_token))
    mid = sent.json()["id"]
    next_owner, next_token = await new_session(client, "claude-code:next")
    premature = await client.post(f"/api/messages/{mid}/readdress", json={
        "sender_session_id": sender, "recipient_session_id": next_owner,
    }, headers=auth(sender_token))
    assert premature.status_code == 409
    ack = await client.post(f"/api/messages/{mid}/acknowledge", json={
        "recipient_session_id": old}, headers=auth(old_token))
    assert ack.status_code == 200
    assert (await client.patch(f"/api/sessions/{old}", json={
        "status": "completed"}, headers=auth(old_token))).status_code == 200
    denied = await client.post(f"/api/messages/{mid}/readdress", json={
        "sender_session_id": sender, "recipient_session_id": next_owner,
    }, headers=auth(sender_token))
    assert denied.status_code == 409
    assert mid not in [m["id"] for m in await inbox(client, next_owner, next_token)]
    status = await client.get(f"/api/messages/{mid}", params={
        "sender_session_id": sender}, headers=auth(sender_token))
    assert status.json()["acknowledged_at"] == ack.json()["acknowledged_at"]
    assert status.json()["original_recipient_session_id"] == old


@pytest.mark.asyncio
async def test_duplicate_successor_claims_have_one_winner(client):
    sender, sender_token = await new_session(client, "codex:sender")
    mid = await send_ticket(client, sender, sender_token)
    a, b = await asyncio.gather(new_session(client, "claude-code:a"),
                                new_session(client, "claude-code:b"))
    a_rows = [m for m in await inbox(client, *a) if m["id"] == mid]
    b_rows = [m for m in await inbox(client, *b) if m["id"] == mid]
    assert len(a_rows) + len(b_rows) == 1
    row = (a_rows or b_rows)[0]
    assert row["sender_session_id"] == sender
    assert len([e for e in row["delivery_history"] if e["action"] == "assigned"]) == 1


@pytest.mark.asyncio
async def test_completed_sender_can_readdress_with_its_original_capability(client):
    sender, sender_token = await new_session(client, "codex:sender")
    old, old_token = await new_session(client, "claude-code:old")
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": old, "body": "direct",
    }, headers=auth(sender_token))
    mid = sent.json()["id"]
    assert (await client.patch(f"/api/sessions/{sender}", json={
        "status": "completed"}, headers=auth(sender_token))).status_code == 200
    assert (await client.patch(f"/api/sessions/{old}", json={
        "status": "completed"}, headers=auth(old_token))).status_code == 200
    next_owner, next_token = await new_session(client, "claude-code:next")
    moved = await client.post(f"/api/messages/{mid}/readdress", json={
        "sender_session_id": sender, "recipient_session_id": next_owner,
    }, headers=auth(sender_token))
    assert moved.status_code == 200, moved.text
    assert moved.json()["original_recipient_session_id"] == old
    assert mid in [m["id"] for m in await inbox(client, next_owner, next_token)]


@pytest.mark.asyncio
async def test_message_writes_keep_receipt_and_owner_compare_and_set(client, monkeypatch):
    """A guard deletion must fail even when a sequential happy path still passes."""
    sender, sender_token = await new_session(client, "codex:sender")
    direct_old, old_token = await new_session(client, "claude-code:old")
    direct = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": direct_old,
        "body": "direct",
    }, headers=auth(sender_token))
    ticket_id = await send_ticket(client, sender, sender_token)
    owner, owner_token = await new_session(client, "claude-code:ticket-owner")
    direct_next, _ = await new_session(client, "claude-code:next")
    writes = []
    original_execute = AsyncSession.execute

    async def record_update(db, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "agent_messages":
            writes.append(str(statement))
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", record_update)
    ack = await client.post(f"/api/messages/{direct.json()['id']}/acknowledge", json={
        "recipient_session_id": direct_old}, headers=auth(old_token))
    assert ack.status_code == 200
    ack_write = writes[-1]
    assert "agent_messages.recipient_session_id =" in ack_write
    assert "agent_messages.acknowledged_at IS NULL" in ack_write

    writes.clear()
    assert (await client.patch(f"/api/sessions/{owner}", json={
        "status": "completed"}, headers=auth(owner_token))).status_code == 200
    assert writes
    release_write = writes[0]
    assert "agent_messages.recipient_session_id =" in release_write
    assert "agent_messages.acknowledged_at IS NULL" in release_write
    assert ticket_id

    writes.clear()
    assert (await client.patch(f"/api/sessions/{direct_old}", json={
        "status": "completed"}, headers=auth(old_token))).status_code == 200
    # An acknowledged direct message is not movable; create a fresh unread one.
    unread_old, unread_token = await new_session(client, "claude-code:unread")
    unread = await client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": unread_old,
        "body": "move once",
    }, headers=auth(sender_token))
    assert (await client.patch(f"/api/sessions/{unread_old}", json={
        "status": "completed"}, headers=auth(unread_token))).status_code == 200
    writes.clear()
    moved = await client.post(f"/api/messages/{unread.json()['id']}/readdress", json={
        "sender_session_id": sender, "recipient_session_id": direct_next,
    }, headers=auth(sender_token))
    assert moved.status_code == 200, moved.text
    move_write = writes[-1]
    assert "agent_messages.recipient_session_id =" in move_write
    assert "agent_messages.acknowledged_at IS NULL" in move_write


@pytest.mark.asyncio
async def test_ticket_claim_guards_both_selection_and_compare_and_set(client, monkeypatch):
    sender, sender_token = await new_session(client, "codex:sender")
    mid = await send_ticket(client, sender, sender_token)
    statements = []
    original_execute = AsyncSession.execute

    async def record_claim(db, statement, *args, **kwargs):
        if isinstance(statement, (Select, Update)) and "agent_messages" in str(statement):
            statements.append(str(statement))
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", record_claim)
    owner, owner_token = await new_session(client, "claude-code:claimant")
    assert mid in [m["id"] for m in await inbox(client, owner, owner_token)]
    claim_statements = [sql for sql in statements if "sessions.creator_uid =" in sql]
    assert len(claim_statements) >= 2, claim_statements
    for sql in claim_statements:
        assert "agent_messages.ticket_id =" in sql
        assert "agent_messages.addressing_mode =" in sql
        assert "agent_messages.acknowledged_at IS NULL" in sql
        assert "agent_messages.recipient_session_id IS NULL" in sql
        assert "sessions.creator_uid =" in sql
