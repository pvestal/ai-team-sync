"""#3589: the observability feed must not misattribute or silently drop ticket work.

Three shapes seen in real records:

* A reviewer's handoff was written to the ticket MAILBOX on 2026-10-09 (no
  recipient). Turnover assigned it to an owner session created on 2026-10-14.
  The feed rendered the CURRENT recipient at the creation timestamp, so a
  session that did not yet exist appeared to receive it.
* 303 of 342 delegated child sessions carry no ticket_id; the delegation row's
  parent_task names it. Ticket views dropped those children and every
  delegation, commit and file event (none carried a task_id).
* The ticket filter ran after a per-table recency cap, so a ticket's older rows
  vanished whenever newer unrelated activity filled the cap.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ai_team_sync.models import (
    AgentMessage,
    CommitRecord,
    Delegation,
    FileActivity,
    Handoff,
    Session,
)


def _now(offset: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset)


def _session(sid: str, agent: str, *, ticket: int | None, started: int, **extra) -> Session:
    return Session(
        id=sid,
        developer="operator",
        agent=agent,
        scope="[]",
        description="work",
        status="completed",
        repo_root="/repo",
        ticket_id=ticket,
        started_at=_now(started),
        completed_at=_now(started + 5),
        **extra,
    )


@pytest.mark.asyncio
async def test_ticket_mailbox_handoff_is_not_shown_as_received_at_creation(client, db_session):
    reviewer = _session("reviewer", "codex:delegate", ticket=1001, started=-500)
    later_owner = _session("later-owner", "claude-code:later", ticket=1001, started=-100)
    handoff = Handoff(
        id="handoff-pool",
        ticket_id=1001,
        source_session_id=reviewer.id,
        recipient_session_id=later_owner.id,  # current assignee, set by turnover
        verdict="BLOCK: repeat completion creates a fresh event_id",
        blockers='["b1"]',
        next_steps="[]",
        artifacts="[]",
        created_at=_now(-400),
    )
    message = AgentMessage(
        id="handoff-message",
        sender_session_id=reviewer.id,
        recipient_session_id=later_owner.id,
        original_recipient_session_id=None,
        addressing_mode="ticket",
        delivery_history=json.dumps(
            [
                {
                    "action": "assigned",
                    "recipient_session_id": later_owner.id,
                    "reason": "ticket_claim",
                    "at": _now(-100).isoformat(),
                }
            ]
        ),
        ticket_id=1001,
        kind="handoff",
        handoff_id=handoff.id,
        sender_agent=reviewer.agent,
        sender_developer="operator",
        recipient_agent=later_owner.agent,
        body="private",
        created_at=_now(-400),
    )
    db_session.add_all([reviewer, later_owner, handoff, message])
    await db_session.commit()

    payload = (
        await client.get("/api/observability/events", params={"limit": 50, "task_id": 1001})
    ).json()
    events = {event["id"]: event for event in payload["events"]}

    for event_id in (
        "handoff:handoff-pool:created",
        "handoff:handoff-pool:blockers",
        "message:handoff-message:created",
    ):
        target = events[event_id]["target"]
        assert target["session_id"] is None, event_id
        assert target["agent"] == "ticket #1001 mailbox", event_id
    created = events["message:handoff-message:created"]
    assert created["detail"]["delivery_history_count"] == 1

    detail = (
        await client.get(
            "/api/observability/events",
            params={
                "limit": 1,
                "event_id": "handoff:handoff-pool:created",
                "include_detail": "true",
                "history_hours": 1,
            },
        )
    ).json()["events"][0]["detail"]
    assert detail["delivery_history"] == [
        {
            "action": "assigned",
            "recipient_session_id": later_owner.id,
            "recipient_agent": later_owner.agent,
            "reason": "ticket_claim",
            "at": json.loads(message.delivery_history)[0]["at"],
        }
    ]
    assert detail["current_recipient"]["session_id"] == later_owner.id


@pytest.mark.asyncio
async def test_direct_message_keeps_its_original_recipient_after_readdress(client, db_session):
    sender = _session("sender", "claude-code:s", ticket=1001, started=-300)
    first = _session("first", "codex:delegate", ticket=1001, started=-290)
    second = _session("second", "codex:delegate", ticket=1001, started=-100)
    message = AgentMessage(
        id="direct",
        sender_session_id=sender.id,
        recipient_session_id=second.id,
        original_recipient_session_id=first.id,
        addressing_mode="session",
        delivery_history="[]",
        ticket_id=1001,
        kind="message",
        sender_agent=sender.agent,
        sender_developer="operator",
        recipient_agent=second.agent,
        body="private",
        created_at=_now(-250),
    )
    db_session.add_all([sender, first, second, message])
    await db_session.commit()

    payload = (
        await client.get("/api/observability/events", params={"limit": 50, "task_id": 1001})
    ).json()
    created = {e["id"]: e for e in payload["events"]}["message:direct:created"]
    assert created["target"]["session_id"] == first.id


@pytest.mark.asyncio
async def test_delegated_child_without_ticket_is_attributed_through_parent_task(client, db_session):
    owner = _session("owner", "claude-code:owner", ticket=1001, started=-300)
    child = _session("child", "codex:delegate", ticket=None, started=-200, delegation_id="deleg-1")
    delegation = Delegation(
        id="deleg-1",
        parent_session_id=owner.id,
        parent_task="1001",
        delegating_worker="claude-code",
        delegated_worker="codex",
        resolved_binary="/usr/bin/codex",
        launch_spec_version="1",
        mode="VERIFY",
        repo_root="/repo",
        scope="[]",
        objective="review",
        acceptance="verdict",
        prohibitions="[]",
        child_session_id=child.id,
        lease_expires_at=_now(60),
        state="closed",
        verdict="ACCEPTED",
        result_summary="BLOCK",
        created_at=_now(-201),
        closed_at=_now(-150),
    )
    commit = CommitRecord(
        id="commit-1",
        session_id=owner.id,
        commit_hash="9d2f7187069",
        message="fix",
        created_at=_now(-120),
    )
    activity = FileActivity(
        id="activity-1",
        session_id=child.id,
        agent=child.agent,
        developer="operator",
        action="read",
        path="src/x.py",
        repo_root="/repo",
        created_at=_now(-190),
    )
    db_session.add_all([owner, child, delegation, commit, activity])
    await db_session.commit()

    sessions = (await client.get("/api/observability/sessions", params={"limit": 10})).json()[
        "sessions"
    ]
    by_id = {row["id"]: row for row in sessions}
    assert by_id[child.id]["task_id"] == 1001
    assert by_id[child.id]["ticket_id"] is None  # the stored column is reported as stored

    payload = (
        await client.get("/api/observability/events", params={"limit": 50, "task_id": 1001})
    ).json()
    ids = {event["id"] for event in payload["events"]}
    assert {
        "session:child:started",
        "delegation:deleg-1:created",
        "delegation:deleg-1:closed",
        "commit:commit-1:created",
        "file_activity:activity-1:created",
    } <= ids


@pytest.mark.asyncio
async def test_ticket_filter_is_applied_before_the_recency_cap(client, db_session):
    owner = _session("owner", "claude-code:owner", ticket=1001, started=-5000)
    other = _session("other", "claude-code:other", ticket=9999, started=-4000)
    old_edit = FileActivity(
        id="ticket-edit",
        session_id=owner.id,
        agent=owner.agent,
        developer="operator",
        action="edit",
        path="src/a.py",
        repo_root="/repo",
        created_at=_now(-4900),
    )
    noise = [
        FileActivity(
            id=f"noise-{i:03d}",
            session_id=other.id,
            agent=other.agent,
            developer="operator",
            action="read",
            path="src/b.py",
            repo_root="/repo",
            created_at=_now(-3000 + i),
        )
        for i in range(150)
    ]
    db_session.add_all([owner, other, old_edit, *noise])
    await db_session.commit()

    payload = (
        await client.get("/api/observability/events", params={"limit": 10, "task_id": 1001})
    ).json()
    ids = {event["id"] for event in payload["events"]}
    assert "file_activity:ticket-edit:created" in ids
    assert not any(event_id.startswith("file_activity:noise") for event_id in ids)
