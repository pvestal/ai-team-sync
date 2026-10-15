"""#3537: completion identity and outbox delivery under retries, races and crashes.

Answers Codex BLOCK c4be209e (ATS handoff f457fd4b):
  A1 a repeated completion must not mint a new event (PATCH retry, POST /complete
     retry, simultaneous completions); a resurrected session's NEXT completion is
     a genuinely new lifecycle and does get its own event;
  A2 switching transport off (ATS_EMIT_COMPLETION=0) must never skip the outbox;
  A3 a row is claimed before it is sent, so two delivery passes cannot both send
     it, a crashed sender's claim expires and the event is retried with the SAME
     event_id, and a sender whose claim expired cannot overwrite the new outcome.

Delivery is at-least-once by design: a send whose response is lost is sent again
under the same event_id, and Echo's intake is idempotent on it.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync import completion_events as ce
from ai_team_sync.models import AgentMessage, Base, CompletionOutbox, Session
from ai_team_sync.routers import sessions as sessions_router

from .test_completion_events import HANDOFF, _outbox, _start
from .test_echo_isolation_3537 import _spawn

SOURCE = Path(__file__).resolve().parents[1] / "src"


# ── helpers ───────────────────────────────────────────────────────────────

class SlowEcho:
    """A stand-in Echo that holds each request open, counts event_ids and can
    answer a chosen status per event."""

    def __init__(self, delay: float = 0.4, status_for=None):
        self.hits: list[str] = []
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.hits.append(body["event_id"])
                time.sleep(delay)
                self.send_response((status_for or (lambda e: 202))(body["event_id"]))
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def counts(self) -> collections.Counter:
        with self._lock:
            return collections.Counter(self.hits)

    def close(self):
        self.server.shutdown()


async def _file_engine(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def _row(event_id: str, created: datetime) -> CompletionOutbox:
    payload = {"event_id": event_id, "session_id": event_id.rsplit("/", 1)[-1]}
    return CompletionOutbox(event_id=event_id, session_id=payload["session_id"],
                            completion_reason=ce.OWNER_COMPLETED, has_handoff=False,
                            payload=json.dumps(payload), payload_sha256=ce.sha256(payload),
                            created_at=created, next_attempt_at=created)


async def _seed(engine, n: int, prefix: str = "ev") -> list[str]:
    base = datetime.now(timezone.utc) - timedelta(minutes=1)
    ids = [f"{prefix}/{i:03d}" for i in range(n)]
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add_all(_row(e, base + timedelta(milliseconds=i)) for i, e in enumerate(ids))
        await db.commit()
    return ids


async def _deliver(engine, **kw):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as db:
        return await ce.deliver_pending(db, **kw)


@pytest.fixture
def real_transport(monkeypatch):
    """Delivery on, file databases are not ephemeral, the real HTTP send."""
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "1")
    monkeypatch.setattr(ce, "ephemeral_database", lambda: False)

    async def post(url, payload):
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(url, json=payload)).status_code

    monkeypatch.setattr(ce, "_post", post)


# ── A1: one completion, one event ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_completion_retries_keep_one_event_one_timestamp_one_notice(
        client, db_session, monkeypatch):
    notices: list[str] = []

    async def count_dispatch(kind, payload):
        if kind == "session.completed":
            notices.append(payload["session_id"])

    monkeypatch.setattr(sessions_router, "dispatch", count_dispatch)
    sid, token = await _start(client, agent="claude-code:worker")  # the ticket's owner
    await _start(client, agent="codex:peer")   # a live peer that receives ticket events
    h = {"X-ATS-Approval-Token": token}
    first = await client.patch(f"/api/sessions/{sid}", headers=h, json={
        "status": "completed", "summary": "done", "handoff": HANDOFF})
    assert first.status_code == 200, first.text
    again = await client.patch(f"/api/sessions/{sid}", headers=h,
                               json={"status": "completed", "summary": "done"})
    alias = await client.post(f"/api/sessions/{sid}/complete", headers=h,
                              json={"summary": "done"})
    assert again.status_code == 200 and alias.status_code == 200, (again.text, alias.text)
    stamps = {r.json()["completed_at"] for r in (first, again, alias)}
    assert len(stamps) == 1, f"completed_at restamped by a retry: {stamps}"
    assert len(await _outbox(db_session, sid)) == 1
    db_session.expire_all()
    events = (await db_session.execute(select(AgentMessage).where(
        AgentMessage.sender_session_id == sid, AgentMessage.kind == "event"))).scalars().all()
    assert len(events) == 1, "a retry re-announced the completion to ticket peers"
    assert notices == [sid]


@pytest.mark.asyncio
async def test_retry_cannot_attach_a_handoff_to_a_completed_session(client, db_session):
    sid, token = await _start(client)
    h = {"X-ATS-Approval-Token": token}
    assert (await client.patch(f"/api/sessions/{sid}", headers=h,
                               json={"status": "completed"})).status_code == 200
    late = await client.post(f"/api/sessions/{sid}/complete", headers=h,
                             json={"handoff": HANDOFF})
    assert late.status_code == 409
    (row,) = await _outbox(db_session, sid)
    assert row.has_handoff is False


@pytest.mark.asyncio
async def test_resurrection_starts_a_new_lifecycle_with_a_new_identity(client, db_session):
    sid, token = await _start(client)
    h = {"X-ATS-Approval-Token": token}
    reaped_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db_session.execute(update(Session).where(Session.id == sid).values(
        status="completed", auto_completed=True, completed_at=reaped_at))
    await db_session.commit()
    await ce.record_completion(db_session, await db_session.get(Session, sid), ce.REAPED)
    await db_session.commit()
    revived = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert revived.status_code == 200 and revived.json()["status"] == "active", revived.text
    for _ in range(3):   # the new lifecycle's completion, then two retries of it
        r = await client.patch(f"/api/sessions/{sid}", headers=h,
                               json={"status": "completed", "summary": "back"})
        assert r.status_code == 200, r.text
    rows = sorted(await _outbox(db_session, sid), key=lambda r: r.created_at)
    assert [r.completion_reason for r in rows] == ["reaped", "owner_completed"]
    assert rows[0].event_id != rows[1].event_id


@pytest.mark.parametrize("ticket", [None, 9903537])
def test_simultaneous_completions_record_one_event(tmp_path, ticket):
    """Real ats-server, file database, eight requests released together."""
    proc, url = _spawn(tmp_path)
    try:
        with httpx.Client(base_url=url, timeout=10) as c:
            made = c.post("/api/sessions", json={
                "developer": "pvestal", "agent": "claude-code:race", "scope": [],
                **({"ticket_id": ticket, "ticket_role": "owner"} if ticket else {})})
            assert made.status_code == 201, made.text
            sid, token = made.json()["id"], made.headers["X-ATS-Approval-Token"]
        barrier = threading.Barrier(8)
        results: list[int] = []

        def complete(i):
            with httpx.Client(base_url=url, timeout=30) as c:
                barrier.wait()
                h = {"X-ATS-Approval-Token": token}
                r = (c.patch(f"/api/sessions/{sid}", headers=h, json={"status": "completed"})
                     if i % 2 else c.post(f"/api/sessions/{sid}/complete", headers=h, json={}))
                results.append(r.status_code)

        threads = [threading.Thread(target=complete, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [200] * 8, results
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    with sqlite3.connect(tmp_path / "ats.db") as conn:
        rows = conn.execute("SELECT event_id FROM completion_outbox WHERE session_id=?",
                            (sid,)).fetchall()
    assert len(rows) == 1, rows


# ── A2: transport off never skips the outbox ─────────────────────────────

@pytest.mark.asyncio
async def test_transport_off_records_holds_and_later_delivers(
        client, db_session, completion_capture, monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    sid, token = await _start(client)
    r = await client.patch(f"/api/sessions/{sid}", json={
        "status": "completed", "handoff": HANDOFF}, headers={"X-ATS-Approval-Token": token})
    assert r.status_code == 200, r.text
    (row,) = await _outbox(db_session, sid)
    assert row.has_handoff and row.delivered_at is None and row.attempts == 0
    assert (await ce.deliver_pending(db_session))["delivered"] == 0
    assert completion_capture == []
    health = await ce.health(db_session)
    assert health["pending"] == 1 and health["delivery_enabled"] is False
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "1")
    await ce.deliver_pending(db_session)
    assert [p["event_id"] for _u, p in completion_capture] == [row.event_id]


# ── A3: claimed delivery ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_concurrent_passes_send_each_event_once(tmp_path, real_transport, monkeypatch):
    """kick() and the background loop are two passes over one file database."""
    echo = SlowEcho(delay=0.3)
    monkeypatch.setenv("ECHO_BRAIN_URL", echo.url)
    a = await _file_engine(tmp_path / "ats.db")
    b = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}")
    try:
        ids = await _seed(a, 6)
        out = await asyncio.gather(_deliver(a), _deliver(b))
    finally:
        echo.close()
        await a.dispose()
        await b.dispose()
    assert echo.counts() == collections.Counter({e: 1 for e in ids}), echo.counts()
    assert sum(o["delivered"] for o in out) == 6


_DELIVER_SCRIPT = """
import asyncio, os, sys
from ai_team_sync import completion_events as ce
from ai_team_sync.database import async_session
if os.environ.get("CRASH_ON_SEND"):
    async def crash(url, payload):
        os._exit(9)
    ce._post = crash
async def main():
    async with async_session() as db:
        print(await ce.deliver_pending(db))
asyncio.run(main())
"""


def _deliver_process(tmp_path, echo_url, **extra):
    env = dict(os.environ, PYTHONPATH=str(SOURCE), ATS_EMIT_COMPLETION="1",
               ECHO_BRAIN_URL=echo_url,
               DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}", **extra)
    return subprocess.Popen([sys.executable, "-c", _DELIVER_SCRIPT], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.asyncio
async def test_two_processes_send_each_event_once(tmp_path):
    echo = SlowEcho(delay=0.3)
    engine = await _file_engine(tmp_path / "ats.db")
    ids = await _seed(engine, 6)
    await engine.dispose()
    try:
        procs = [_deliver_process(tmp_path, echo.url) for _ in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        assert all(p.returncode == 0 for p in procs), outs
    finally:
        echo.close()
    assert echo.counts() == collections.Counter({e: 1 for e in ids}), (echo.counts(), outs)


@pytest.mark.asyncio
async def test_a_crashed_sender_is_retried_with_the_same_event_id(tmp_path, real_transport,
                                                                  monkeypatch):
    engine = await _file_engine(tmp_path / "ats.db")
    (eid,) = await _seed(engine, 1)
    crashed = _deliver_process(tmp_path, "http://127.0.0.1:9", CRASH_ON_SEND="1")
    crashed.communicate(timeout=60)
    assert crashed.returncode == 9, "the sender did not die mid-send"
    echo = SlowEcho(delay=0)
    monkeypatch.setenv("ECHO_BRAIN_URL", echo.url)
    try:
        held = await _deliver(engine)
        assert echo.counts() == collections.Counter(), "a live claim was ignored"
        later = datetime.now(timezone.utc) + timedelta(seconds=ce.CLAIM_LEASE_S + 1)
        retried = await _deliver(engine, now=later)
    finally:
        echo.close()
    assert held["delivered"] == 0 and retried["delivered"] == 1, (held, retried)
    assert echo.counts() == collections.Counter({eid: 1})
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        row = await db.get(CompletionOutbox, eid)
    await engine.dispose()
    assert row.delivered_at is not None and row.attempts == 2 and row.claim_token is None


@pytest.mark.asyncio
async def test_an_expired_claim_cannot_overwrite_the_newer_outcome(tmp_path, real_transport,
                                                                   monkeypatch):
    """Sender A stalls past its lease; B re-claims and delivers; A's late
    failure must not reopen B's delivered row."""
    engine = await _file_engine(tmp_path / "ats.db")
    (eid,) = await _seed(engine, 1)
    release_a = asyncio.Event()
    calls = 0

    async def post(url, payload):
        nonlocal calls
        calls += 1
        if calls == 1:            # A: hang, then fail
            await release_a.wait()
            return 503
        return 202                # B

    monkeypatch.setattr(ce, "_post", post)
    a = asyncio.create_task(_deliver(engine))
    while calls == 0:
        await asyncio.sleep(0.01)
    later = datetime.now(timezone.utc) + timedelta(seconds=ce.CLAIM_LEASE_S + 1)
    b = await _deliver(engine, now=later)
    release_a.set()
    a = await a
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        row = await db.get(CompletionOutbox, eid)
    await engine.dispose()
    assert b["delivered"] == 1 and a["lost_claim"] == 1, (a, b)
    assert row.delivered_at is not None and row.last_error is None


# ── poisoned events and queue progress ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_event_does_not_hold_back_the_others(tmp_path, real_transport,
                                                             monkeypatch):
    engine = await _file_engine(tmp_path / "ats.db")
    ids = await _seed(engine, 60)
    poisoned = set(ids[:55])          # more failures than one pass takes
    echo = SlowEcho(delay=0, status_for=lambda e: 500 if e in poisoned else 202)
    monkeypatch.setenv("ECHO_BRAIN_URL", echo.url)
    try:
        first = await _deliver(engine)
        second = await _deliver(engine)
        third = await _deliver(engine)
    finally:
        echo.close()
    assert first["failed"] == 50 and second["delivered"] == 5 and second["failed"] == 5
    assert third["due"] == 0, "a failed event was retried before its backoff"
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        health = await ce.health(db)
    await engine.dispose()
    assert health["delivered"] == 5 and health["delivery_failing"] == 55


@pytest.mark.asyncio
async def test_a_rejected_event_stays_visible_and_can_be_requeued(tmp_path, real_transport,
                                                                  monkeypatch):
    engine = await _file_engine(tmp_path / "ats.db")
    (eid,) = await _seed(engine, 1)
    verdict = {"status": 422}
    echo = SlowEcho(delay=0, status_for=lambda e: verdict["status"])
    monkeypatch.setenv("ECHO_BRAIN_URL", echo.url)
    try:
        assert (await _deliver(engine))["rejected"] == 1
        assert (await _deliver(engine))["due"] == 0
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            assert (await ce.health(db))["rejected"] == 1
            assert await ce.requeue(db, [eid]) == 1
            assert await ce.requeue(db, ["no-such-event"]) == 0
        verdict["status"] = 202
        assert (await _deliver(engine))["delivered"] == 1
    finally:
        echo.close()
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        row = await db.get(CompletionOutbox, eid)
        assert await ce.requeue(db, [eid]) == 0, "a delivered event was requeued"
    await engine.dispose()
    assert row.delivered_at is not None and row.rejected_at is None
    assert echo.counts()[eid] == 2
