"""Read-only operator projections for the Tower Agent Console."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ai_team_sync.models import (
    AgentMessage,
    AuthorityCheck,
    Decision,
    Delegation,
    FileActivity,
    Handoff,
    OverrideRequest,
    Session,
)


def _now(offset: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset)


@pytest.mark.asyncio
async def test_session_projection_exposes_lineage_mode_and_health(client, db_session):
    parent = Session(
        id="parent-session",
        developer="operator",
        agent="claude-code:parent",
        scope=json.dumps(["src/**"]),
        description="parent work",
        status="active",
        repo_root="/repo",
        ticket_id=3522,
        started_at=_now(-20),
        last_heartbeat=_now(-2),
    )
    child = Session(
        id="child-session",
        developer="operator",
        agent="codex:delegate",
        scope=json.dumps([]),
        description="review",
        status="active",
        repo_root="/repo",
        task_id=3522,
        ticket_id=3522,
        delegation_id="delegation-1",
        started_at=_now(-10),
        last_heartbeat=_now(-1),
    )
    delegation = Delegation(
        id="delegation-1",
        parent_session_id=parent.id,
        parent_task="3522",
        delegating_worker="claude-code",
        delegated_worker="codex",
        resolved_binary="/usr/bin/codex",
        launch_spec_version="1",
        mode="READ_ONLY",
        repo_root="/repo",
        scope="[]",
        objective="verify",
        acceptance="find defects",
        prohibitions='["no writes"]',
        child_session_id=child.id,
        lease_expires_at=_now(120),
        state="open",
        created_at=_now(-11),
    )
    db_session.add_all([parent, child, delegation])
    await db_session.commit()

    response = await client.get("/api/observability/sessions", params={"limit": 10})
    assert response.status_code == 200
    payload = response.json()
    rows = {row["id"]: row for row in payload["sessions"]}

    assert rows[parent.id]["child_session_ids"] == [child.id]
    assert rows[child.id]["parent_session_id"] == parent.id
    assert rows[child.id]["effective_mode"] == "READ_ONLY"
    assert rows[child.id]["task_id"] == 3522
    assert rows[child.id]["ats_health"]["registered"] is True
    assert rows[child.id]["ats_health"]["scoped"] is False
    assert rows[child.id]["ats_health"]["brief"] == "UNINSTRUMENTED"
    assert rows[child.id]["ats_health"]["client_connection"] == "UNINSTRUMENTED"
    assert rows[child.id]["effective_authority"]["edit"] == "none"
    assert rows[child.id]["description"] == "WITHHELD_BY_DEFAULT"


@pytest.mark.asyncio
async def test_event_projection_is_bounded_directional_and_omits_message_body(client, db_session):
    parent = Session(
        id="parent",
        developer="operator",
        agent="claude-code:one",
        scope="[]",
        description="work",
        status="active",
        repo_root="/repo",
        ticket_id=3522,
        started_at=_now(-30),
    )
    child = Session(
        id="child",
        developer="operator",
        agent="codex:two",
        scope="[]",
        description="review",
        status="active",
        repo_root="/repo",
        task_id=3522,
        ticket_id=3522,
        started_at=_now(-29),
    )
    message = AgentMessage(
        id="message-1",
        sender_session_id=parent.id,
        recipient_session_id=child.id,
        original_recipient_session_id=child.id,
        addressing_mode="session",
        delivery_history="[]",
        ticket_id=3522,
        kind="message",
        sender_agent=parent.agent,
        sender_developer="operator",
        recipient_agent=child.agent,
        body="do not expose this private message body sk-supersecret123456789012345",
        created_at=_now(-20),
    )
    handoff = Handoff(
        id="handoff-1",
        ticket_id=3522,
        source_session_id=parent.id,
        recipient_session_id=child.id,
        verdict="BLOCK",
        blockers='["missing evidence"]',
        next_steps='["verify tests"]',
        artifacts='["commit:abc"]',
        created_at=_now(-19),
    )
    unstarted_delegation = Delegation(
        id="delegation-requested",
        parent_session_id=parent.id,
        parent_task="3522",
        delegating_worker="claude-code",
        delegated_worker="codex",
        mode="VERIFY",
        repo_root="/repo",
        scope="[]",
        objective="independent review",
        acceptance="return findings",
        prohibitions="[]",
        child_session_id=None,
        lease_expires_at=_now(300),
        state="open",
        created_at=_now(-18),
    )
    decision = Decision(
        id="decision-1",
        session_id=parent.id,
        ticket_id=3522,
        title="Keep the console read-only",
        chosen="No mutations",
        reasoning="Operator boundary ghp_abcdefghijklmnopqrstuvwxyz",
        files="[]",
        created_at=_now(-18),
    )
    authority = AuthorityCheck(
        id="authority-1",
        session_id=child.id,
        agent=child.agent,
        worker="codex",
        action="task_close",
        task_id=3522,
        allowed=False,
        reasons='["READ_ONLY delegation"]',
        created_at=_now(-17),
    )
    activity = FileActivity(
        id="activity-1",
        session_id=child.id,
        agent=child.agent,
        developer="operator",
        action="read",
        path="src/example.py",
        repo_root="/repo",
        created_at=_now(-16),
    )
    override = OverrideRequest(
        id="override-1",
        requester_session_id=child.id,
        owner_session_id=parent.id,
        conflicting_pattern="src/**",
        justification="verify",
        status="denied",
        response_message="read-only lane",
        created_at=_now(-15),
        responded_at=_now(-14),
        expires_at=_now(300),
    )
    db_session.add_all(
        [
            parent,
            child,
            message,
            handoff,
            unstarted_delegation,
            decision,
            authority,
            activity,
            override,
        ]
    )
    await db_session.commit()

    response = await client.get("/api/observability/events", params={"limit": 100})
    assert response.status_code == 200
    payload = response.json()
    events = payload["events"]
    assert len(events) <= 100
    assert payload["next_cursor"]

    by_id = {event["id"]: event for event in events}
    message_event = by_id["message:message-1:created"]
    assert message_event["event_type"] == "HANDOFF"
    assert message_event["source"]["session_id"] == parent.id
    assert message_event["target"]["session_id"] == child.id
    assert "body" not in json.dumps(message_event).lower()
    assert "supersecret" not in json.dumps(payload)

    assert by_id["handoff:handoff-1:created"]["status"] == "BLOCK"
    assert by_id["handoff:handoff-1:blockers"]["event_type"] == "BLOCKER"
    requested = by_id["delegation:delegation-requested:created"]
    assert requested["event_type"] == "TASK"
    assert requested["target"] is None
    assert by_id["authority:authority-1:checked"]["event_type"] == "RULING"
    assert by_id["authority:authority-1:checked"]["status"] == "denied"
    assert by_id["file_activity:activity-1:created"]["event_type"] == "TOOL"
    assert by_id["override:override-1:responded"]["status"] == "denied"
    assert by_id["decision:decision-1:created"]["summary"] == "ATS decision recorded"
    assert "chosen" not in by_id["decision:decision-1:created"]["detail"]

    expanded = await client.get(
        "/api/observability/events",
        params={
            "limit": 1,
            "event_id": "decision:decision-1:created",
            "include_detail": True,
        },
    )
    assert expanded.status_code == 200
    assert expanded.json()["events"][0]["detail"]["chosen"] == "No mutations"
    assert "ghp_" not in json.dumps(expanded.json())
    assert "[REDACTED]" in expanded.json()["events"][0]["detail"]["reasoning"]

    rejected_expansion = await client.get(
        "/api/observability/events", params={"include_detail": True}
    )
    assert rejected_expansion.status_code == 422


@pytest.mark.asyncio
async def test_event_cursor_observes_late_completion_of_old_session(client, db_session):
    baseline = Session(
        id="baseline",
        developer="operator",
        agent="codex:baseline",
        scope="[]",
        description="baseline",
        status="active",
        started_at=_now(-10),
    )
    old = Session(
        id="old-session",
        developer="operator",
        agent="claude-code:old",
        scope="[]",
        description="old",
        status="active",
        started_at=_now(-(8 * 24 * 60 * 60)),
    )
    db_session.add_all([baseline, old])
    await db_session.commit()
    initial = (await client.get("/api/observability/events", params={"limit": 10})).json()

    old.status = "completed"
    old.completed_at = _now(10)
    await db_session.commit()
    resumed = (
        await client.get(
            "/api/observability/events",
            params={"limit": 10, "cursor": initial["next_cursor"]},
        )
    ).json()
    ids = {event["id"] for event in resumed["events"]}
    assert "session:old-session:completed" in ids
    assert "session:old-session:started" not in ids


@pytest.mark.asyncio
async def test_live_cursor_drains_burst_without_skipping_oldest_rows(client, db_session):
    session = Session(
        id="burst-session",
        developer="operator",
        agent="codex:burst",
        scope="[]",
        description="burst",
        status="active",
        started_at=_now(-20),
    )
    db_session.add(session)
    await db_session.commit()
    initial = (await client.get("/api/observability/events", params={"limit": 1})).json()

    activities = [
        FileActivity(
            id=f"burst-{index:03d}",
            session_id=session.id,
            agent=session.agent,
            developer="operator",
            action="read",
            path=f"src/{index:03d}.py",
            repo_root="/repo",
            created_at=_now(1),
        )
        for index in range(220)
    ]
    db_session.add_all(activities)
    await db_session.commit()

    cursor = initial["next_cursor"]
    seen: set[str] = set()
    for _ in range(5):
        page = (
            await client.get("/api/observability/events", params={"limit": 100, "cursor": cursor})
        ).json()
        seen.update(
            event["id"] for event in page["events"] if event["id"].startswith("file_activity:")
        )
        cursor = page["next_cursor"]
        if not page["events"]:
            break
    assert len(seen) == 220


@pytest.mark.asyncio
async def test_event_cursor_resumes_without_replaying_older_records(client, db_session):
    first = Session(
        id="first",
        developer="operator",
        agent="codex:first",
        scope="[]",
        description="first",
        status="active",
        started_at=_now(-30),
    )
    second = Session(
        id="second",
        developer="operator",
        agent="codex:second",
        scope="[]",
        description="second",
        status="active",
        started_at=_now(-20),
    )
    db_session.add_all([first, second])
    await db_session.commit()

    initial = (await client.get("/api/observability/events", params={"limit": 1})).json()
    assert len(initial["events"]) == 1
    assert initial["events"][0]["id"] == "session:second:started"

    third = Session(
        id="third",
        developer="operator",
        agent="claude-code:third",
        scope="[]",
        description="third",
        status="active",
        started_at=_now(-10),
    )
    db_session.add(third)
    await db_session.commit()

    resumed = (
        await client.get(
            "/api/observability/events",
            params={"limit": 10, "cursor": initial["next_cursor"]},
        )
    ).json()
    assert all(event["id"] != initial["events"][0]["id"] for event in resumed["events"])
    assert any(event["id"] == "session:third:started" for event in resumed["events"])


@pytest.mark.asyncio
async def test_coverage_names_known_invisible_boundaries(client):
    response = await client.get("/api/observability/coverage")
    assert response.status_code == 200
    coverage = response.json()["event_types"]
    assert coverage["ATS"]["state"] == "OBSERVED"
    assert coverage["HANDOFF"]["state"] == "OBSERVED"
    assert coverage["TOOL"]["state"] == "PARTIAL"
    assert coverage["USER"]["state"] == "UNINSTRUMENTED"
    assert coverage["CLAUDE"]["state"] == "PARTIAL"
    assert coverage["CODEX"]["state"] == "PARTIAL"
