"""#3537: one v1 completion event per completed session, durable before delivery.

The ATS half of the ATS -> Echo contract. tests/fixtures/ats_session_completed_v1.json
is byte-identical to Echo's copy; both suites pin it.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select, update

from ai_team_sync import completion_events as ce
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import CompletionOutbox, Handoff, Session

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ats_session_completed_v1.json")
                     .read_text(encoding="utf-8"))


async def _start(client, ticket_id=3537, agent="claude-code:test"):
    r = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": [], "repo_root": "/repo",
        "ticket_id": ticket_id})
    assert r.status_code == 201, r.text
    return r.json()["id"], r.headers["X-ATS-Approval-Token"]


async def _complete(client, handoff=None, summary="finished"):
    sid, token = await _start(client)
    body = {"status": "completed", "summary": summary}
    if handoff is not None:
        body["handoff"] = handoff
    r = await client.patch(f"/api/sessions/{sid}", json=body,
                           headers={"X-ATS-Approval-Token": token})
    assert r.status_code == 200, r.text
    return sid


async def _outbox(db, sid=None):
    q = select(CompletionOutbox)
    if sid:
        q = q.where(CompletionOutbox.session_id == sid)
    db.expire_all()
    return (await db.execute(q)).scalars().all()


HANDOFF = {"verdict": "SHIPPED — exact “text”", "blockers": ["none really"],
           "next_steps": ["replay", "watch health"], "artifacts": ["commit 0000000"]}


# ── contract ──────────────────────────────────────────────────────────────

def test_builder_reproduces_the_shared_fixture_byte_for_byte():
    ev = FIXTURE["event"]
    h = ev["handoff"]
    session = Session(id=ev["session_id"], developer="x", agent=ev["agent"],
                      repo_root=ev["repo_root"], summary=ev["summary"], ticket_id=ev["ticket_id"],
                      task_id=ev["task_id"],
                      completed_at=datetime.fromisoformat(ev["completed_at"]).replace(tzinfo=None))
    handoff = Handoff(id=h["handoff_id"], ticket_id=h["ticket_id"], source_session_id=ev["session_id"],
                      verdict=h["verdict"], blockers=json.dumps(h["blockers"]),
                      next_steps=json.dumps(h["next_steps"]), artifacts=json.dumps(h["artifacts"]),
                      created_at=datetime.fromisoformat(h["created_at"]))
    built = ce.build_event(session, handoff, ce.OWNER_COMPLETED, source=ev["source"])
    assert built == ev
    assert ce.sha256(built) == FIXTURE["payload_sha256"]
    assert built["handoff"]["content_sha256"] == FIXTURE["handoff_content_sha256"]


def test_event_id_is_stable_per_logical_completion():
    s = Session(id="s", developer="x", agent="a", completed_at=datetime(2026, 10, 9, 20, 0, 0, 1))
    a = ce.build_event(s, None, ce.REAPED, source={})
    b = ce.build_event(s, None, ce.REAPED, source={})
    assert a["event_id"] == b["event_id"] == "ats.session.completed/v1/s/2026-10-09T20:00:00.000001+00:00"
    s.completed_at = datetime(2026, 10, 9, 21, 0, 0)
    assert ce.build_event(s, None, ce.REAPED, source={})["event_id"] != a["event_id"]


# ── 1. the test suite cannot reach live Echo ─────────────────────────────

def test_suite_points_echo_at_the_tripwire(echo_tripwire):
    assert ce.echo_url() == echo_tripwire.url
    assert ce.ephemeral_database() is True
    assert ce.DEFAULT_ECHO_URL == "http://localhost:8309"  # what used to leak


@pytest.mark.asyncio
async def test_in_memory_ats_never_sends_even_with_the_real_transport(client, db_session, monkeypatch):
    """Undo the conftest recorder and aim at a real listener: nothing arrives."""
    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(202)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ECHO_BRAIN_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("ATS_EMIT_COMPLETION", "1")
        monkeypatch.setattr(ce, "_post", _real_post)
        sid = await _complete(client, HANDOFF)
        result = await ce.deliver_pending(db_session)
        await asyncio.sleep(0.2)
    finally:
        server.shutdown()
    assert hits == []
    assert result["suppressed"] == 1
    (row,) = await _outbox(db_session, sid)
    assert row.delivered_at is None and "ephemeral" in row.suppressed_reason


async def _real_post(url, payload):
    import httpx
    async with httpx.AsyncClient(timeout=5) as c:
        return (await c.post(url, json=payload)).status_code


# ── 2/3. ordinary completion ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_completion_with_handoff_is_in_the_outbox_before_delivery(client, db_session, completion_capture):
    sid = await _complete(client, HANDOFF)
    (row,) = await _outbox(db_session, sid)
    ev = json.loads(row.payload)
    assert row.delivered_at is None and row.has_handoff
    assert ev["completion_reason"] == "owner_completed" and ev["ticket_id"] == 3537
    assert ev["handoff"]["verdict"] == HANDOFF["verdict"]
    assert ev["handoff"]["next_steps"] == HANDOFF["next_steps"]
    assert ev["handoff"]["content_sha256"] == ce.handoff_content_sha256(ev["handoff"])
    assert ev["event_id"] == ce.event_id_for(sid, ev["completed_at"])
    assert ce.sha256(ev) == row.payload_sha256

    out = await ce.deliver_pending(db_session)
    assert out["delivered"] == 1
    assert completion_capture == [(f"{ce.echo_url()}/api/ats/session-completed", ev)]
    (row,) = await _outbox(db_session, sid)
    assert row.delivered_at is not None and row.attempts == 1


@pytest.mark.asyncio
async def test_completion_without_handoff_carries_null(client, db_session, completion_capture):
    sid = await _complete(client)
    (row,) = await _outbox(db_session, sid)
    assert json.loads(row.payload)["handoff"] is None and not row.has_handoff


# ── 4. the reaper emits the same contract ─────────────────────────────────

@pytest.mark.asyncio
async def test_reaped_session_emits_a_v1_event(db_session, completion_capture):
    sess = Session(developer="x", agent="codex:idle", ticket_id=3537, repo_root="/repo",
                   started_at=datetime.now(timezone.utc)
                   - timedelta(hours=settings.session_inactivity_hours + 1))
    db_session.add(sess)
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    (row,) = await _outbox(db_session, sess.id)
    ev = json.loads(row.payload)
    assert ev["completion_reason"] == "reaped" and ev["handoff"] is None
    assert ev["event_version"] == 1 and set(ev) == set(FIXTURE["event"])
    await ce.deliver_pending(db_session)
    assert [p["event_id"] for _u, p in completion_capture] == [ev["event_id"]]


@pytest.mark.asyncio
async def test_reaped_then_owner_completed_with_handoff_is_a_second_event(client, db_session, completion_capture):
    sid, token = await _start(client)
    await db_session.execute(update(Session).where(Session.id == sid).values(
        status="completed", auto_completed=True,
        completed_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db_session.commit()
    await ce.record_completion(db_session, await db_session.get(Session, sid), ce.REAPED)
    await db_session.commit()
    r = await client.patch(f"/api/sessions/{sid}", json={
        "status": "completed", "summary": "came back", "handoff": HANDOFF},
        headers={"X-ATS-Approval-Token": token})
    assert r.status_code == 200, r.text
    rows = sorted(await _outbox(db_session, sid), key=lambda r: r.created_at)
    assert [r.completion_reason for r in rows] == ["reaped", "owner_completed"]
    assert rows[0].event_id != rows[1].event_id and rows[1].has_handoff


# ── 5. Echo down: visible, retried, same event_id ─────────────────────────

@pytest.mark.asyncio
async def test_echo_down_is_retried_with_backoff_and_the_same_event_id(client, db_session,
                                                                      completion_capture, monkeypatch):
    sid = await _complete(client, HANDOFF)
    calls: list[str] = []

    async def down(url, payload):
        calls.append(payload["event_id"])
        raise ConnectionError("Echo restarting")
    monkeypatch.setattr(ce, "_post", down)
    out = await ce.deliver_pending(db_session)
    assert out["failed"] == 1
    (row,) = await _outbox(db_session, sid)
    assert row.delivered_at is None and row.attempts == 1 and "Echo restarting" in row.last_error
    h = await ce.health(db_session)
    assert h["pending"] == 1 and h["delivery_failing"] == 1
    assert h["last_error"]["event_id"] == row.event_id

    assert (await ce.deliver_pending(db_session))["due"] == 0  # backing off, not hammering
    await db_session.execute(update(CompletionOutbox).values(
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db_session.commit()
    async def flaky(url, payload):
        calls.append(payload["event_id"])
        return 503
    monkeypatch.setattr(ce, "_post", flaky)
    assert (await ce.deliver_pending(db_session))["failed"] == 1
    (row,) = await _outbox(db_session, sid)
    assert row.attempts == 2 and row.last_error == "HTTP 503"

    await db_session.execute(update(CompletionOutbox).values(
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db_session.commit()
    monkeypatch.setattr(ce, "_post", lambda url, payload: _ok(calls, payload))
    assert (await ce.deliver_pending(db_session))["delivered"] == 1
    assert len(set(calls)) == 1 and len(calls) == 3  # one logical event, three tries
    h = await ce.health(db_session)
    assert h["pending"] == 0 and h["delivered"] == 1 and h["retries"] == 2


async def _ok(calls, payload):
    calls.append(payload["event_id"])
    return 202


# ── 6. duplicates ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recording_the_same_completion_twice_keeps_one_row(client, db_session, completion_capture):
    sid = await _complete(client)
    s = await db_session.get(Session, sid)
    await ce.record_completion(db_session, s, ce.OWNER_COMPLETED)
    await db_session.commit()
    assert len(await _outbox(db_session, sid)) == 1
    await ce.deliver_pending(db_session)
    await ce.deliver_pending(db_session)
    assert len(completion_capture) == 1  # a delivered row is never resent


@pytest.mark.asyncio
async def test_a_rejected_event_is_not_retried_and_is_visible(client, db_session,
                                                             completion_capture, monkeypatch):
    sid = await _complete(client)

    async def conflict(url, payload):
        return 409
    monkeypatch.setattr(ce, "_post", conflict)
    assert (await ce.deliver_pending(db_session))["rejected"] == 1
    (row,) = await _outbox(db_session, sid)
    assert row.rejected_at is not None
    assert (await ce.health(db_session))["rejected"] == 1


# ── completion never waits on Echo ────────────────────────────────────────

@pytest.mark.asyncio
async def test_completion_succeeds_and_releases_locks_while_echo_is_down(client, db_session,
                                                                        completion_capture, monkeypatch):
    async def down(url, payload):
        raise ConnectionError("down")
    monkeypatch.setattr(ce, "_post", down)
    sid = await _complete(client, HANDOFF)
    s = await client.get(f"/api/sessions/{sid}")
    assert s.json()["status"] == "completed"
    assert len(await _outbox(db_session, sid)) == 1


@pytest.mark.asyncio
async def test_emission_switched_off_records_nothing(client, db_session, monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    sid = await _complete(client)
    assert await _outbox(db_session, sid) == []


# ── 12. historical replay ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_builds_live_identical_events_once(client, db_session, completion_capture, monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")  # the history predates the outbox
    with_h = await _complete(client, HANDOFF)
    without = await _complete(client)
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "1")
    assert await _outbox(db_session) == []

    plan = await ce.replay_plan(db_session)
    assert [e["session_id"] for e in plan] == [with_h]  # handoffs only by default
    s = await db_session.get(Session, with_h)
    h = (await db_session.execute(select(Handoff).where(
        Handoff.source_session_id == with_h))).scalar_one()
    assert plan[0] == ce.build_event(s, h, ce.OWNER_COMPLETED, source=plan[0]["source"])
    assert await ce.replay_apply(db_session, plan) == 1
    assert await ce.replay_plan(db_session) == []  # rerun finds nothing new
    assert [e["session_id"] for e in await ce.replay_plan(db_session, handoffs_only=False)] == [without]
    (row,) = await _outbox(db_session, with_h)
    assert row.replayed
    await ce.deliver_pending(db_session)
    assert [p["event_id"] for _u, p in completion_capture] == [plan[0]["event_id"]]
