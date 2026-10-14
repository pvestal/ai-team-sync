"""D3 (operator ruling 2026-10-14): reading task information must not silently claim work.

A brief is a read. It must not bind a session to a ticket, and it must not take
the ticket's queued handoffs away from the session that later claims the ticket
explicitly with start_session(ticket_id=...).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.test_task_brief import _exact_envelope

TICKET = 4101
STATE_TABLES = ("sessions", "agent_messages", "handoffs", "scope_locks", "decisions")


@pytest.fixture(autouse=True)
def _resolve_prose_to_ticket(monkeypatch):
    import ai_team_sync.briefs as briefs

    monkeypatch.setattr(
        briefs, "resolve_tower_task",
        lambda objective, **kw: ({"status": "resolved", "task_id": TICKET,
                                  "confidence": 0.91, "candidates": []}, None),
        raising=False,
    )
    _exact_envelope(monkeypatch)


async def _snapshot(db_engine) -> dict:
    async with db_engine.connect() as conn:
        return {table: (await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))).all()
                for table in STATE_TABLES}


async def _start(client, agent: str, ticket_id: int | None = None) -> tuple[str, str]:
    body = {"developer": "patrick", "agent": agent, "scope": [],
            "description": agent, "repo_root": "/repo"}
    if ticket_id is not None:
        body["ticket_id"] = ticket_id
    response = await client.post("/api/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"], response.headers["X-ATS-Approval-Token"]


async def _queue_handoff(client) -> None:
    sid, token = await _start(client, "codex:predecessor", TICKET)
    done = await client.patch(f"/api/sessions/{sid}", json={
        "status": "completed", "summary": "predecessor complete",
        "handoff": {"verdict": "Continue exact residual"},
    }, headers={"X-ATS-Approval-Token": token})
    assert done.status_code == 200, done.text


async def _brief(client, sid: str, token: str, **extra):
    response = await client.post("/api/brief", json={
        "objective": "continue the synthetic task A residual", "session_id": sid, **extra,
    }, headers={"X-ATS-Approval-Token": token})
    assert response.status_code == 200, response.text
    return response.json()


async def _queued_mail(db_engine) -> list:
    async with db_engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT m.recipient_session_id, m.acknowledged_at, m.delivery_history, "
            "h.recipient_session_id FROM agent_messages m JOIN handoffs h ON h.id = m.handoff_id "
            "WHERE m.ticket_id = :t AND m.kind = 'handoff'"), {"t": TICKET})).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"task_id": TICKET}, {}], ids=["exact_task_id", "prose_resolved"])
async def test_reading_a_brief_does_not_claim_the_ticket(client, db_engine, extra):
    sid, token = await _start(client, "codex:reader")

    packet = await _brief(client, sid, token, **extra)

    assert packet["task_id"] == TICKET
    assert packet["session_linkage"] == {
        "status": "not_bound", "task_id": TICKET,
        "claim_with": f"start_session(ticket_id={TICKET})"}
    assert "TICKET NOT CLAIMED" in packet["rendered"]
    assert (await client.get(f"/api/sessions/{sid}")).json()["ticket_id"] is None


@pytest.mark.asyncio
async def test_reading_a_brief_does_not_consume_or_reassign_ticket_mail(client, db_engine):
    await _queue_handoff(client)
    before = await _queued_mail(db_engine)
    assert len(before) == 1 and before[0][0] is None and before[0][3] is None

    sid, token = await _start(client, "codex:reader")
    await _brief(client, sid, token, task_id=TICKET)

    assert await _queued_mail(db_engine) == before
    inbox = await client.get(f"/api/sessions/{sid}/messages",
                             headers={"X-ATS-Approval-Token": token})
    assert inbox.status_code == 200, inbox.text
    assert inbox.json() == []


@pytest.mark.asyncio
async def test_repeated_brief_reads_leave_all_coordination_state_unchanged(client, db_engine):
    await _queue_handoff(client)
    sid, token = await _start(client, "codex:reader")
    before = await _snapshot(db_engine)

    for extra in ({"task_id": TICKET}, {}, {"task_id": TICKET}):
        await _brief(client, sid, token, **extra)
        assert await _snapshot(db_engine) == before


@pytest.mark.asyncio
async def test_explicit_successor_receives_queued_handoff_after_others_read_briefs(
        client, db_engine):
    await _queue_handoff(client)
    reader, reader_token = await _start(client, "codex:reader")
    await _brief(client, reader, reader_token, task_id=TICKET)
    await _brief(client, reader, reader_token)

    successor, successor_token = await _start(client, "codex:successor", TICKET)

    inbox = await client.get(f"/api/sessions/{successor}/messages",
                             headers={"X-ATS-Approval-Token": successor_token})
    assert any(m["kind"] == "handoff" and "Continue exact residual" in m["body"]
               for m in inbox.json()), inbox.text
    [(recipient, acked, _history, handoff_recipient)] = await _queued_mail(db_engine)
    assert recipient == successor and handoff_recipient == successor and acked is None
    reader_inbox = await client.get(f"/api/sessions/{reader}/messages",
                                    headers={"X-ATS-Approval-Token": reader_token})
    assert reader_inbox.json() == []


@pytest.mark.asyncio
async def test_authorized_claim_still_binds_and_reads_report_existing_linkage(client, db_engine):
    owner, owner_token = await _start(client, "codex:owner", TICKET)
    assert (await client.get(f"/api/sessions/{owner}")).json()["ticket_id"] == TICKET
    assert (await _brief(client, owner, owner_token, task_id=TICKET))["session_linkage"][
        "status"] == "already_bound"

    other, other_token = await _start(client, "codex:other-ticket", 4102)
    before = await _snapshot(db_engine)
    linkage = (await _brief(client, other, other_token, task_id=TICKET))["session_linkage"]
    assert linkage == {"status": "bound_to_other_ticket", "task_id": TICKET,
                       "session_ticket_id": 4102}
    assert await _snapshot(db_engine) == before
