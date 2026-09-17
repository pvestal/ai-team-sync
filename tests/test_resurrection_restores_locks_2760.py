"""#2760 — a resurrected session gets its lane back, or is told why it did not.

THE REGRESSION. Resurrection landed in b99ddc3 (2026-08-10), when reaping did
NOT release locks: a reaped session kept them for the full lock_ttl_hours, so
there was nothing to restore and the path was coherent. 06d91b9 (2026-08-25)
correctly fixed that lingering lane — and thereby gave resurrection something to
put back. The resurrect branch was never updated, so it restored status,
completed_at, auto_completed and the summary marker, and no locks. The board
then showed an ACTIVE claim, scope intact, holding nothing.

THE BOUNDARY:
  - reap still deletes the rows; the lane is genuinely free while the session is
    dead.
  - restoration is re-acquisition under the ordinary overlap rule (#2756), from
    the reaper's JOURNAL and never reconstructed from `scope`.
  - a legitimate newer holder always wins.
  - restoration never mints a fresh TTL, and never restores an expired lock.

WHAT THESE TESTS ASSERT ON. Structured fields — `locks_restored`,
`locks_not_restored`, `restoration_outcome`/`restoration_reason`, and the
session.resurrected event — never the human summary. The summary carries counts
and a refusal code only, and a test that greps prose for a lock pattern is a
test that forces patterns into prose.

Both revival doors are covered: the heartbeat and PATCH {"status": "active"}.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_team_sync.background_tasks import auto_complete_stale_sessions, check_expired_locks
from ai_team_sync.config import settings
from ai_team_sync.models import Base, ScopeLock, Session

ROOT = "/srv/echo-2760"

# Every valid-but-awkward glob the marker used to mangle. They are lock
# PATTERNS, not prose, and nothing in the narrative path may special-case them.
AWKWARD_GLOBS = ["src/[ab]*.py", "src/[[]x.py", "a/[x[y]z.py", "src/]x.py"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _long_ago() -> datetime:
    return _utcnow() - timedelta(hours=settings.session_inactivity_hours + 24)


async def _silent_session(db, *, agent="claude-code:owner", scope=(), locks=(),
                          repo_root=ROOT, identified=True, **extra):
    """An active session that has gone silent, holding `locks`."""
    if identified:
        extra.setdefault("creator_uid", os.getuid())
    sess = Session(developer="pvestal", agent=agent, repo_root=repo_root,
                   scope=json.dumps(list(scope)), status="active",
                   started_at=_long_ago(), last_heartbeat=_long_ago(), **extra)
    db.add(sess)
    await db.flush()
    for pattern, mode in locks:
        db.add(ScopeLock(session_id=sess.id, pattern=pattern, mode=mode,
                         reason="2760", created_at=_long_ago(),
                         expires_at=_utcnow() + timedelta(hours=4)))
    await db.commit()
    return sess


async def _locks_of(db, session_id):
    db.expire_all()
    rows = (await db.execute(
        select(ScopeLock).where(ScopeLock.session_id == session_id))).scalars().all()
    return {l.pattern: l.mode for l in rows}


async def _all_locks(db):
    db.expire_all()
    return (await db.execute(select(ScopeLock))).scalars().all()


async def _row(db, session_id) -> Session:
    db.expire_all()
    return (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()


async def _journal_of(db, session_id) -> str:
    return (await _row(db, session_id)).reaped_locks or ""


async def _set_journal(db, session_id, payload) -> None:
    row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
    row.reaped_locks = payload if isinstance(payload, str) else json.dumps(payload)
    await db.commit()


async def _heartbeat(client, session_id):
    return await client.post(f"/api/sessions/{session_id}/heartbeat")


# ONE door. PATCH revival is refused outright (see the refusal section below),
# so `door` exists only to keep the behavioural tests reading as "revive it".
door = pytest.fixture()(lambda: _heartbeat)


# ── core semantics ───────────────────────────────────────────────────────

async def test_reap_then_revival_restores_the_lane_it_took(client, db_session, door):
    sess = await _silent_session(
        db_session, scope=["src/**", "docs/**"],
        locks=[("src/**", "advisory"), ("docs/**", "exclusive")])
    sid = sess.id

    assert await auto_complete_stale_sessions(db_session) == 1
    assert await _locks_of(db_session, sid) == {}   # reap freed the lane

    resp = await door(client, sid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert sorted(body["locks_restored"]) == ["docs/**", "src/**"]
    assert body["locks_not_restored"] == [] and body["restoration_reason"] == ""
    assert body["restoration_outcome"] == "restored"
    assert body["lock_count"] == 2

    assert await _locks_of(db_session, sid) == {
        "src/**": "advisory", "docs/**": "exclusive"}


async def test_reap_still_deletes_locks_while_the_session_is_dead(db_session):
    await _silent_session(db_session, scope=["src/**"], locks=[("src/**", "exclusive")])
    assert await auto_complete_stale_sessions(db_session) == 1
    assert await _all_locks(db_session) == []


async def test_scope_alone_never_becomes_a_lock(client, db_session, door):
    sess = await _silent_session(db_session, scope=["src/**", "never/held/**"],
                                 locks=[("src/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    resp = await door(client, sid)
    assert resp.json()["locks_restored"] == ["src/**"]
    assert await _locks_of(db_session, sid) == {"src/**": "advisory"}


async def test_restoration_does_not_mint_a_fresh_ttl(client, db_session, door):
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    original = _utcnow() + timedelta(minutes=17)
    db_session.add(ScopeLock(session_id=sid, pattern="src/**", mode="advisory",
                             reason="2760", created_at=_long_ago(), expires_at=original))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await door(client, sid)).status_code == 200

    db_session.expire_all()
    lock = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalar_one()
    got = lock.expires_at if lock.expires_at.tzinfo else lock.expires_at.replace(tzinfo=timezone.utc)
    assert abs((got - original).total_seconds()) < 2, "restoration extended the claim"


# ── 1. expiry, decided without consulting the resurrected status ─────────

@pytest.mark.parametrize("mode", ["advisory", "exclusive"])
@pytest.mark.parametrize("age_hours,restorable", [
    (0, True),                                     # live: expires in the future
    (-0.1, False),                                 # just expired
    (-(settings.lock_ttl_hours + 1), False),       # past a full TTL
    (-720, False),                                 # thirty days dead
])
async def test_an_expired_lock_is_never_restored_whatever_its_mode(
        client, db_session, door, mode, age_hours, restorable):
    """NON-TAUTOLOGICAL BY CONSTRUCTION: the same call is asserted to restore a
    live lock and to refuse expired ones, so deleting the expiry check flips the
    three False rows and deleting the restore flips the True one.

    The earlier version of this repair mirrored check_expired_locks' live-owner
    exemption — and heartbeat sets status='active' BEFORE restoration runs, so
    `live_exclusive_owner` was unconditionally true, the branch was dead, and an
    exclusive lock thirty days past its TTL was re-minted, counted live by
    _get_active_locks, and then never collected by the sweep because its owner
    was active. lock_ttl_hours defeated by one reap/resurrect cycle.
    """
    expires = _utcnow() + timedelta(hours=1) if age_hours == 0 \
        else _utcnow() + timedelta(hours=age_hours)
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    db_session.add(ScopeLock(session_id=sid, pattern="src/**", mode=mode,
                             reason="2760", created_at=_long_ago(), expires_at=expires))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1

    resp = await door(client, sid)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    if restorable:
        assert body["locks_restored"] == ["src/**"], body
        assert await _locks_of(db_session, sid) == {"src/**": mode}
    else:
        assert body["locks_restored"] == [], body
        assert body["locks_not_restored"] == ["src/**"], body
        assert await _locks_of(db_session, sid) == {}


async def test_an_expired_exclusive_lock_does_not_become_immortal(client, db_session):
    """The consequence the expiry check exists to prevent, asserted end to end
    against the production sweep: nothing survives that check_expired_locks
    would not itself have kept."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    db_session.add(ScopeLock(session_id=sid, pattern="src/**", mode="exclusive",
                             reason="2760", created_at=_long_ago(),
                             expires_at=_utcnow() - timedelta(days=30)))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await _heartbeat(client, sid)).status_code == 200

    assert await _locks_of(db_session, sid) == {}
    # And the sweep agrees there is nothing left to collect.
    assert await check_expired_locks(db_session) == 0
    assert await _all_locks(db_session) == []


async def test_the_sweep_still_keeps_a_live_owners_expired_exclusive_lock(db_session):
    """The exemption restoration deliberately does NOT copy is untouched where
    it belongs: a session that never stopped being live keeps its lock (#2741)."""
    live = await _silent_session(db_session, agent="claude-code:live", scope=["a/**"])
    live.last_heartbeat = _utcnow()
    db_session.add(ScopeLock(session_id=live.id, pattern="a/**", mode="exclusive",
                             reason="2760", created_at=_long_ago(),
                             expires_at=_utcnow() - timedelta(minutes=5)))
    await db_session.commit()
    assert await check_expired_locks(db_session) == 0
    assert await _locks_of(db_session, live.id) == {"a/**": "exclusive"}


# ── 2. owner completion is fail-closed on both doors ─────────────────────

@pytest.mark.parametrize("closer", ["patch", "complete_alias"])
async def test_owner_completion_after_reap_permanently_blocks_resurrection(
        client, db_session, closer):
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    assert await _journal_of(db_session, sid) != "", "precondition: journal armed"

    payload = {"summary": "handed off, lane released"}
    done = await (client.patch(f"/api/sessions/{sid}", json={"status": "completed", **payload})
                  if closer == "patch"
                  else client.post(f"/api/sessions/{sid}/complete", json=payload))
    assert done.status_code == 200, done.text
    assert await _journal_of(db_session, sid) == "", "owner completion left the journal armed"

    late = await _heartbeat(client, sid)
    assert late.status_code == 409 and "completed by its owner" in late.text

    assert await _locks_of(db_session, sid) == {}
    row = await _row(db_session, sid)
    assert row.status == "completed" and "lane released" in (row.summary or "")


async def test_the_callee_refuses_an_owner_completed_session_called_directly(db_session):
    """The caller's 409 is not the only thing standing between an owner's "done"
    and its journal."""
    from ai_team_sync.routers.sessions import restore_reaped_locks

    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    outcome = await restore_reaped_locks(
        db_session, await _row(db_session, sid), was_auto_completed=False)
    await db_session.commit()

    assert outcome.restored == [] and outcome.reason == "owner_completed"
    assert await _locks_of(db_session, sid) == {}
    assert await _journal_of(db_session, sid) == ""


# ── 3. PATCH IS NOT A DOOR: every revival transition is refused ─────────

async def _armed_reaped_session(db, **kw):
    sess = await _silent_session(db, scope=["src/**", "docs/**"],
                                 locks=[("src/**", "exclusive"), ("docs/**", "advisory")], **kw)
    assert await auto_complete_stale_sessions(db) == 1
    assert await _journal_of(db, sess.id) != "", "precondition: journal armed"
    return sess.id


@pytest.mark.parametrize("payload", [
    {"status": "active"},
    {"status": "paused"},
    {"status": "active", "repo_root": "/srv/OTHER"},
    {"status": "active", "summary": "back at it"},
    {"status": "active", "scope": ["totally/new/**"]},
    {"status": "active", "description": "rewritten"},
    {"status": "paused", "repo_root": "/srv/OTHER", "summary": "x",
     "scope": ["other/**"], "description": "y"},
])
async def test_patch_cannot_revive_a_session_holding_an_armed_journal(
        client, db_session, payload):
    """Restoration lives on the heartbeat and nowhere else.

    Putting an authority-granting operation inside a multi-field mutator
    produced three defects with one shape, because the other fields are applied
    AFTER it: `repo_root` re-anchored the session after restoration had
    conflict-checked the old anchor, leaving it holding a lane in a namespace it
    never worked in; `summary` overwrote the marker recording the restoration
    the same request had just performed; and pause/resume never matched the
    completed->active shape at all. The fix is not to reorder the assignments —
    it is that PATCH does not revive.
    """
    sid = await _armed_reaped_session(db_session)
    before = await _row(db_session, sid)
    was = (before.status, before.repo_root, before.summary, before.scope,
           before.description, before.reaped_locks, before.auto_completed)

    resp = await client.patch(f"/api/sessions/{sid}", json=payload)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "restoration_requires_heartbeat"
    assert "heartbeat" in detail["message"]

    # ATOMIC: not one submitted field was applied.
    after = await _row(db_session, sid)
    assert (after.status, after.repo_root, after.summary, after.scope,
            after.description, after.reaped_locks, after.auto_completed) == was
    assert await _locks_of(db_session, sid) == {}, "a refused PATCH granted authority"


async def test_the_pause_resume_pair_cannot_launder_a_revival(client, db_session):
    """The third door, reached through live surfaces: `ats session pause` and
    the MCP pause_session/resume_session tools PATCH status directly. Matching
    only completed->active let reap -> paused -> active through, which revived
    the session with zero locks and left the journal armed for nobody."""
    sid = await _armed_reaped_session(db_session)

    paused = await client.patch(f"/api/sessions/{sid}", json={"status": "paused"})
    assert paused.status_code == 409, paused.text
    assert (await _row(db_session, sid)).status == "completed"

    resumed = await client.patch(f"/api/sessions/{sid}", json={"status": "active"})
    assert resumed.status_code == 409, resumed.text
    assert (await _row(db_session, sid)).status == "completed"
    assert await _locks_of(db_session, sid) == {}


async def test_the_refusal_leaves_the_journal_spendable_by_a_heartbeat(
        client, db_session):
    """A refusal must not consume the evidence a legitimate revival needs."""
    sid = await _armed_reaped_session(db_session)
    journal_before = await _journal_of(db_session, sid)

    for payload in ({"status": "active"}, {"status": "paused"},
                    {"status": "active", "repo_root": "/srv/OTHER"}):
        assert (await client.patch(f"/api/sessions/{sid}", json=payload)).status_code == 409
    assert await _journal_of(db_session, sid) == journal_before, "a refusal ate the journal"

    body = (await _heartbeat(client, sid)).json()
    assert body["restoration_outcome"] == "restored"
    assert sorted(body["locks_restored"]) == ["docs/**", "src/**"]
    assert await _locks_of(db_session, sid) == {
        "src/**": "exclusive", "docs/**": "advisory"}


async def test_an_ordinary_patch_on_a_reaped_session_still_works(client, db_session):
    """Only REVIVAL is refused. A PATCH that does not try to make the session
    run again keeps ordinary semantics."""
    sid = await _armed_reaped_session(db_session)
    resp = await client.patch(f"/api/sessions/{sid}", json={"description": "annotated"})
    assert resp.status_code == 200, resp.text
    assert (await _row(db_session, sid)).description == "annotated"
    assert await _journal_of(db_session, sid) != ""


async def test_owner_completion_is_not_a_revival_and_is_still_allowed(client, db_session):
    """PATCH status=completed on a reaped session is the owner saying done. It
    must pass, and it must disarm the journal permanently."""
    sid = await _armed_reaped_session(db_session)
    resp = await client.patch(f"/api/sessions/{sid}",
                              json={"status": "completed", "summary": "done"})
    assert resp.status_code == 200, resp.text
    assert await _journal_of(db_session, sid) == ""
    assert (await _heartbeat(client, sid)).status_code == 409


# ── 4. the anchor moved ─────────────────────────────────────────────────

@pytest.mark.parametrize("new_root", ["/srv/UNRELATED", ""])
async def test_restoration_is_refused_when_the_anchor_moved(
        client, db_session, door, new_root):
    """A restored lock has no anchor of its own — readers derive it from the
    session's CURRENT repo_root. Re-anchoring to '' is the wider case: per
    docs/lock-readers.md an unanchored lock means that path in EVERY repo."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await client.patch(f"/api/sessions/{sid}",
                               json={"repo_root": new_root})).status_code == 200

    resp = await door(client, sid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["restoration_reason"] == "anchor_moved"
    assert await _locks_of(db_session, sid) == {}


async def test_restoration_survives_a_no_op_re_anchor(client, db_session, door):
    """A PATCH restating the SAME anchor, trailing slash and all, is not a move."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await client.patch(f"/api/sessions/{sid}",
                               json={"repo_root": ROOT + "/"})).status_code == 200

    resp = await door(client, sid)
    assert resp.json()["locks_restored"] == ["src/**"]
    assert await _locks_of(db_session, sid) == {"src/**": "advisory"}


# ── 5. creator_uid NULL is an unconditional refusal ─────────────────────

async def test_legacy_row_without_creator_uid_is_refused_restoration(
        client, db_session, door):
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")], identified=False)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    resp = await door(client, sid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active", "the session itself still revives"
    assert resp.json()["restoration_reason"] == "unidentified_owner"
    assert await _locks_of(db_session, sid) == {}
    assert await _journal_of(db_session, sid) == "", "a refused journal stayed armed"


async def test_a_foreign_account_cannot_mint_locks_on_a_legacy_row(
        client, db_session, monkeypatch):
    """The attack the refusal closes. The session may still revive — that is the
    pre-existing #2741 gap, ticketed as #2763 — but no lock is minted for it."""
    from ai_team_sync import peer_identity

    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")], identified=False)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    monkeypatch.setattr(peer_identity, "peer_uid_for_request",
                        lambda request: os.getuid() + 1)
    assert (await _heartbeat(client, sid)).status_code == 200
    assert await _all_locks(db_session) == [], "a foreign heartbeat minted a lock"


# ── 6. identity-bound sessions ──────────────────────────────────────────

async def test_identity_bound_session_gets_no_restoration_side_door(client, db_session):
    sess = await _silent_session(db_session, agent="echo-executor", scope=["src/**"],
                                 locks=[("src/**", "exclusive")],
                                 bound_worker="echo-executor", bound_uid=4242)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await _heartbeat(client, sid)).status_code == 409

    assert await _locks_of(db_session, sid) == {}
    assert (await _row(db_session, sid)).status == "completed"


async def test_callee_refuses_a_bound_session_even_when_the_caller_does_not(db_session):
    """#2741's lesson was to enumerate every writer, so the callee is asserted
    directly, with the caller's guard bypassed entirely."""
    from ai_team_sync.routers.sessions import restore_reaped_locks

    sess = await _silent_session(db_session, agent="echo-executor", scope=["src/**"],
                                 locks=[("src/**", "exclusive")],
                                 bound_worker="echo-executor", bound_uid=4242)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    outcome = await restore_reaped_locks(db_session, await _row(db_session, sid),
                                         was_auto_completed=True)
    await db_session.commit()
    assert outcome.restored == [] and outcome.reason == "identity_bound"
    assert await _locks_of(db_session, sid) == {}
    assert await _journal_of(db_session, sid) == ""


# ── 7. malformed / over-limit journal data ──────────────────────────────

@pytest.mark.parametrize("payload,detail", [
    ("not json at all", "readable JSON"),
    ('["bare", "list"]', "not an object"),
    ({"repo_root": ROOT, "locks": "nope"}, "no lock list"),
    ({"repo_root": ROOT, "locks": [{"pattern": 7, "mode": "advisory",
                                    "expires_at": "2099-01-01T00:00:00+00:00"}]},
     "lock contract"),
    ({"repo_root": ROOT, "locks": [{"pattern": "a sentence, not a glob",
                                    "mode": "advisory",
                                    "expires_at": "2099-01-01T00:00:00+00:00"}]},
     "lock contract"),
    ({"repo_root": ROOT, "locks": [{"pattern": "secrets/**", "mode": "EXCLUSIVE",
                                    "expires_at": "2099-01-01T00:00:00+00:00"}]},
     "unknown mode"),
    ({"repo_root": ROOT, "locks": [{"pattern": "**", "mode": "exclusive",
                                    "expires_at": "not-a-date"}]},
     "unreadable expiry"),
])
async def test_poisoned_journal_is_refused_deterministically(
        client, db_session, door, payload, detail):
    """A non-str pattern used to raise AttributeError and 500 the hottest
    endpoint in the system, leaving the session permanently un-resurrectable."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    await _set_journal(db_session, sid, payload)

    resp = await door(client, sid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["restoration_reason"] == "invalid_journal"
    assert detail in body["restoration_detail"], body["restoration_detail"]
    assert await _all_locks(db_session) == [], "poisoned journal minted a lock"


async def test_a_journal_outliving_the_ttl_is_refused(client, db_session):
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    far = _utcnow() + timedelta(days=365)
    await _set_journal(db_session, sid, {
        "repo_root": ROOT,
        "locks": [{"pattern": "**", "mode": "exclusive", "reason": "poison",
                   "expires_at": far.isoformat()}]})

    resp = await _heartbeat(client, sid)
    assert resp.json()["restoration_reason"] == "invalid_journal"
    assert "lock_ttl_hours" in resp.json()["restoration_detail"]
    assert await _all_locks(db_session) == []


async def test_one_bad_entry_refuses_the_whole_journal(client, db_session):
    """No partial authority from invalid input — the alternative is a
    restoration whose result depends on entry ordering."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    ok = (_utcnow() + timedelta(hours=1)).isoformat()
    await _set_journal(db_session, sid, {
        "repo_root": ROOT,
        "locks": [{"pattern": "good/**", "mode": "advisory", "expires_at": ok},
                  {"pattern": "", "mode": "advisory", "expires_at": ok}]})

    assert (await _heartbeat(client, sid)).json()["restoration_reason"] == "invalid_journal"
    assert await _all_locks(db_session) == []


# ── 8. a legitimate newer holder always wins ────────────────────────────

async def _newcomer_holding(db, pattern, mode="exclusive"):
    newcomer = Session(developer="pvestal", agent="codex:newcomer", repo_root=ROOT,
                       creator_uid=os.getuid(), scope=json.dumps([pattern]),
                       status="active")
    db.add(newcomer)
    await db.flush()
    db.add(ScopeLock(session_id=newcomer.id, pattern=pattern, mode=mode,
                     reason="took the free lane",
                     expires_at=_utcnow() + timedelta(hours=4)))
    await db.commit()
    return newcomer.id


async def test_restoration_never_steps_on_a_newer_holder(client, db_session, door):
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    newcomer_id = await _newcomer_holding(db_session, "src/**")

    resp = await door(client, sid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["locks_not_restored"] == ["src/**"]
    assert resp.json()["locks_restored"] == []

    assert await _locks_of(db_session, newcomer_id) == {"src/**": "exclusive"}
    assert await _locks_of(db_session, sid) == {}


async def test_a_partial_restoration_leaves_no_journal_behind(client, db_session):
    sess = await _silent_session(db_session, scope=["src/**", "docs/**"],
                                 locks=[("src/**", "exclusive"), ("docs/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    await _newcomer_holding(db_session, "src/**")

    body = (await _heartbeat(client, sid)).json()
    assert body["locks_restored"] == ["docs/**"]
    assert body["locks_not_restored"] == ["src/**"]
    assert await _journal_of(db_session, sid) == ""
    assert await _locks_of(db_session, sid) == {"docs/**": "advisory"}

    # A retry finds nothing to replay, and the refused lane stays refused.
    assert (await _heartbeat(client, sid)).status_code == 200
    assert await _locks_of(db_session, sid) == {"docs/**": "advisory"}


# ── 9. the journal is consumed exactly once, on every refusal path ──────

@pytest.mark.parametrize("break_it,code", [
    ("anchor", "anchor_moved"),
    ("legacy", "unidentified_owner"),
    ("poison", "invalid_journal"),
])
async def test_a_refused_restoration_consumes_the_journal_exactly_once(
        client, db_session, break_it, code):
    sess = await _silent_session(
        db_session, scope=["src/**"], locks=[("src/**", "exclusive")],
        identified=(break_it != "legacy"))
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    if break_it == "anchor":
        assert (await client.patch(f"/api/sessions/{sid}",
                                   json={"repo_root": "/srv/OTHER"})).status_code == 200
    elif break_it == "poison":
        await _set_journal(db_session, sid, {
            "repo_root": ROOT,
            "locks": [{"pattern": ["not", "a", "string"], "mode": "advisory",
                       "expires_at": (_utcnow() + timedelta(hours=1)).isoformat()}]})

    first = await _heartbeat(client, sid)
    assert first.json()["restoration_reason"] == code
    assert await _journal_of(db_session, sid) == ""

    assert (await _heartbeat(client, sid)).status_code == 200
    assert await _all_locks(db_session) == []


# ── the marker is structural: no pattern ever reaches narrative text ────

@pytest.mark.parametrize("pattern", AWKWARD_GLOBS)
async def test_no_lock_pattern_ever_reaches_the_summary(client, db_session, pattern):
    """Every one of these is a VALID lock pattern. The marker carries counts and
    a refusal code, so none of them needs escaping, none can terminate the
    marker early, and none can graft a fragment onto the operator's own words.
    The patterns are read from the structured field instead."""
    from ai_team_sync.schemas import LockCreate
    LockCreate(session_id="s", pattern=pattern)   # precondition: it IS valid

    narrative = "operator handoff note"
    sess = await _silent_session(db_session, scope=[pattern],
                                 locks=[(pattern, "exclusive")], summary=narrative)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    await _newcomer_holding(db_session, pattern)

    body = (await _heartbeat(client, sid)).json()
    assert body["locks_not_restored"] == [pattern], "the pattern must survive verbatim"
    summary = body["summary"] or ""
    assert pattern not in summary, f"pattern leaked into narrative: {summary!r}"
    assert summary.startswith(narrative), f"narrative mangled: {summary!r}"


@pytest.mark.parametrize("pattern", AWKWARD_GLOBS)
async def test_the_marker_still_dedups_across_repeated_cycles(client, db_session, pattern):
    """#2445: one lifecycle marker per session, however many reap/revive cycles
    run. Asserted per pattern because that is what the previous repair broke."""
    narrative = "operator handoff note"
    sess = await _silent_session(db_session, scope=[pattern],
                                 locks=[(pattern, "advisory")], summary=narrative)
    sid = sess.id
    for _ in range(3):
        row = await _row(db_session, sid)
        row.last_heartbeat = _long_ago()
        row.status = "active"
        # The reaper scores staleness off the MAX of started_at, last heartbeat
        # and the newest lock/commit/decision, so a freshly restored lock counts
        # as activity. Backdate it too, or only the first cycle ever reaps.
        for lock in (await db_session.execute(
                select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all():
            lock.created_at = _long_ago()
        await db_session.commit()
        assert await auto_complete_stale_sessions(db_session) == 1
        assert (await _heartbeat(client, sid)).status_code == 200

    summary = (await _row(db_session, sid)).summary or ""
    assert summary.count("[resurrected") == 1, summary
    assert summary.count("[auto-completed") == 0, summary
    assert summary.startswith(narrative), summary


# ── restoration itself never produces a duplicate row ───────────────────

async def test_restoration_grants_each_lane_exactly_once(client, db_session):
    """#2760 must not add a duplicate-producing path of its own.

    Duplicate stacking exists at HEAD — POST /api/locks inserts unconditionally
    and the overlap rule excludes the session's own rows, so three POSTs for one
    pattern give three rows (#2765, measured with #2760 nowhere in the path).
    That is not this ticket's to fix, but restoration must not ADD to it: one
    journal entry grants at most one row, and a repeated revival grants none.
    """
    sess = await _silent_session(db_session, scope=["src/**", "docs/**"],
                                 locks=[("src/**", "exclusive"), ("docs/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    for _ in range(3):
        assert (await _heartbeat(client, sid)).status_code == 200

    rows = [l.pattern for l in (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all()]
    assert sorted(rows) == ["docs/**", "src/**"], rows


async def test_a_journal_naming_one_lane_twice_restores_exactly_what_was_held(
        client, db_session):
    """A session that stacked duplicates BEFORE the reap has both rows
    journalled, and gets both back. Restoration is FAITHFUL, not narrowing and
    not widening: the name of this test is the invariant, and the invariant is
    "exactly what was held", not "once" — the overlap rule excludes the
    session's own locks, so the second entry is not blocked by the first. If the
    pre-reap state was itself a duplicate stack, that is #2765 reproduced, and
    #2760 must not make it bigger."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    expires = (_utcnow() + timedelta(hours=2)).isoformat()
    created = _long_ago().isoformat()
    await _set_journal(db_session, sid, {
        "repo_root": ROOT,
        "locks": [{"pattern": "src/**", "mode": "exclusive", "reason": "a",
                   "expires_at": expires, "created_at": created},
                  {"pattern": "src/**", "mode": "advisory", "reason": "b",
                   "expires_at": expires, "created_at": created}]})

    body = (await _heartbeat(client, sid)).json()
    rows = [(l.pattern, l.mode) for l in (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all()]
    # EXACTLY what was journalled, which is exactly what was held: two rows in,
    # two rows back. The overlap rule excludes the session's own locks, so the
    # second entry is not blocked by the first — restoration is FAITHFUL to the
    # pre-reap state rather than widening it. If that pre-reap state was itself
    # a duplicate, that is #2765's stacking, reproduced not amplified.
    assert rows == [("src/**", "exclusive"), ("src/**", "advisory")], rows
    assert body["restoration_outcome"] == "restored"
    assert body["locks_restored"] == ["src/**", "src/**"], body["locks_restored"]


# ── restoration and the ordinary door agree about authority ─────────────

@pytest.mark.parametrize("spelling", ["/srv//B", "/srv/B/.", "/srv/B", "/srv/B/"])
async def test_restoration_and_create_lock_agree_on_the_authority_conflict(
        client, db_session, spelling):
    """PARITY. `_cross_repo` compares a caller's anchor against each holder's RAW
    repo_root with nothing but an rstrip, so restoration normalising only its own
    side made it the one claim path whose halves disagreed: anchored '/srv//B' it
    skipped a newcomer's exclusive lock as "another repo" and granted a lane
    POST /api/locks refuses with 409, leaving two exclusive holders of one
    pattern. Asserted for the same session, same holder, same database state —
    whatever the ordinary door says, restoration must say too."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")], repo_root=spelling)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    newcomer = Session(developer="pvestal", agent="codex:newcomer", repo_root=spelling,
                       creator_uid=os.getuid(), scope=json.dumps(["src/**"]),
                       status="active")
    db_session.add(newcomer)
    await db_session.flush()
    db_session.add(ScopeLock(session_id=newcomer.id, pattern="src/**", mode="exclusive",
                             reason="took the free lane",
                             expires_at=_utcnow() + timedelta(hours=4)))
    await db_session.commit()

    restored = (await _heartbeat(client, sid)).json()
    restoration_granted = restored["locks_restored"] == ["src/**"]

    # The ordinary door, for the SAME now-active session and the same lane.
    ordinary = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "src/**", "mode": "exclusive", "reason": "parity"})
    ordinary_granted = ordinary.status_code == 201

    assert restoration_granted == ordinary_granted, (
        f"restoration granted={restoration_granted} but POST /api/locks "
        f"granted={ordinary_granted} ({ordinary.status_code}) for anchor {spelling!r}")
    assert not restoration_granted, "a live exclusive holder must refuse both doors"
    assert restored["not_restored_reasons"] == ["newer_holder"], restored


# ── a reaped session that held NO locks keeps its ordinary lifecycle ────

async def test_a_lockless_reaped_session_can_still_be_paused_and_resumed(
        client, db_session):
    """The reaper writes an envelope for EVERY session it completes, so one that
    held no locks is journalled as `{"repo_root": ..., "locks": []}` — a
    non-empty string. Keying the PATCH refusal on truthiness made every reaped
    session permanently heartbeat-only and broke `ats session pause` and the
    pause_session / resume_session MCP tools, which answer 200 at HEAD.

    The journal here is the one REAL reap writes; nothing is hand-constructed."""
    sess = await _silent_session(db_session, scope=["src/**"])   # no locks held
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    journal = await _journal_of(db_session, sid)
    assert journal, "precondition: reap writes an envelope even with no locks"
    assert json.loads(journal)["locks"] == [], journal

    paused = await client.patch(f"/api/sessions/{sid}", json={"status": "paused"})
    assert paused.status_code == 200, paused.text
    assert (await _row(db_session, sid)).status == "paused"

    resumed = await client.patch(f"/api/sessions/{sid}", json={"status": "active"})
    assert resumed.status_code == 200, resumed.text
    assert (await _row(db_session, sid)).status == "active"


async def test_a_malformed_journal_still_refuses_the_patch_door(client, db_session):
    """Fail closed, exactly as restoration itself does for malformed input.
    Unreadable must not read as 'nothing here' in the one place that decides
    whether PATCH may bypass restoration."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    await _set_journal(db_session, sid, "not json at all")

    resp = await client.patch(f"/api/sessions/{sid}", json={"status": "active"})
    assert resp.status_code == 409, resp.text
    assert (await _row(db_session, sid)).status == "completed"


# ── the restored lock keeps its original clock ──────────────────────────

async def test_a_restored_lock_keeps_its_original_created_at(client, db_session):
    """The reaper scores staleness as max(started_at, last_heartbeat, newest
    lock/commit/decision). A restored lock stamped `now` would buy the session a
    fresh inactivity window it had not earned, so the journal carries the
    original clock and restoration uses it."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    original_created = _long_ago()
    db_session.add(ScopeLock(session_id=sid, pattern="src/**", mode="exclusive",
                             reason="2760", created_at=original_created,
                             expires_at=_utcnow() + timedelta(hours=4)))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await _heartbeat(client, sid)).status_code == 200

    db_session.expire_all()
    lock = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalar_one()
    got = lock.created_at if lock.created_at.tzinfo else lock.created_at.replace(tzinfo=timezone.utc)
    drift = abs((got - original_created).total_seconds())
    assert drift < 2, f"restoration reset the lock clock by {drift:.0f}s"


async def test_restoration_cannot_buy_the_session_another_inactivity_window(
        client, db_session):
    """The consequence, asserted against the reaper itself: a session whose only
    'activity' is a restored lock is still reapable on the next sweep once its
    heartbeat ages out."""
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await _heartbeat(client, sid)).status_code == 200
    assert await _locks_of(db_session, sid) == {"src/**": "exclusive"}

    # Age out only the heartbeat. If the restored lock had taken created_at=now
    # it would still score as fresh activity and hold the lane regardless.
    row = await _row(db_session, sid)
    row.last_heartbeat = _long_ago()
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1, \
        "a restored lock kept a silent session alive past its window"
    assert await _locks_of(db_session, sid) == {}


# ── the five outcome states are distinguishable without reading prose ───

async def test_an_ordinary_heartbeat_reports_not_attempted(client, db_session):
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "advisory")])
    sess.last_heartbeat = _utcnow()
    await db_session.commit()
    body = (await _heartbeat(client, sess.id)).json()
    assert body["restoration_outcome"] == "not_attempted"
    assert body["restoration_reason"] == "" and body["locks_restored"] == []


async def test_a_revival_with_an_empty_journal_says_nothing_was_journalled(
        client, db_session):
    """The 209 auto-completed rows that predate this column resurrect into
    exactly this state, and it must not read as 'restored nothing, no reason'."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    await _set_journal(db_session, sid, "")

    body = (await _heartbeat(client, sid)).json()
    assert body["restoration_outcome"] == "nothing_journalled"
    assert body["restoration_reason"] == ""
    assert body["status"] == "active"


async def test_each_unrestored_lane_says_why(client, db_session):
    """A count cannot tell an owner whether its lane was taken or aged out."""
    sess = await _silent_session(db_session, scope=["taken/**", "stale/**", "kept/**"])
    sid = sess.id
    for pattern, expires in (("taken/**", _utcnow() + timedelta(hours=4)),
                             ("stale/**", _utcnow() - timedelta(minutes=5)),
                             ("kept/**", _utcnow() + timedelta(hours=4))):
        db_session.add(ScopeLock(session_id=sid, pattern=pattern, mode="exclusive",
                                 reason="2760", created_at=_long_ago(), expires_at=expires))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1
    await _newcomer_holding(db_session, "taken/**")

    body = (await _heartbeat(client, sid)).json()
    assert body["restoration_outcome"] == "restored"
    assert body["locks_restored"] == ["kept/**"]
    # PARALLEL, index for index — not a dict that collapses duplicate lanes.
    assert body["locks_not_restored"] == ["taken/**", "stale/**"]
    assert body["not_restored_reasons"] == ["newer_holder", "expired"]
    assert len(body["not_restored_reasons"]) == len(body["locks_not_restored"])


# ── concurrency, on a PRODUCTION-REPRESENTATIVE engine ──────────────────

@pytest.fixture
async def file_backed(tmp_path):
    """A file-backed engine, because the in-memory fixture is a StaticPool: all
    'concurrent' requests there share ONE DBAPI connection, so a loser sees the
    winner's UNCOMMITTED update and contention is never really tested.
    deploy/ats-server.service runs SQLite from a file, journal_mode=delete."""
    from ai_team_sync.database import get_db
    from ai_team_sync.server import create_app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ats.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    app = create_app()

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        async with factory() as probe:
            yield ac, probe, engine
    await engine.dispose()


async def test_the_test_engine_is_not_the_in_memory_artifact(file_backed):
    _client, _probe, engine = file_backed
    assert type(engine.pool).__name__ != "StaticPool", (
        "this concurrency proof must not run on the shared-connection pool")
    async with engine.begin() as conn:
        mode = (await conn.exec_driver_sql("pragma journal_mode")).scalar()
    assert mode == "delete", f"deployment runs journal_mode=delete, fixture is {mode}"


@contextlib.asynccontextmanager
async def _pin_the_first_commit():
    """Hold the FIRST commit that happens inside the block, open.

    THE SEAM HAS TO BE THE COMMIT ITSELF. A seam anywhere earlier — inside the
    restoration evaluation, or just before `db.commit()` — only pins the winner
    in a window the claim already covers, so a mutation that releases the claim
    later than the seam is invisible and the proof passes for the wrong reason.
    That happened twice on this ticket. Wrapping AsyncSession.commit puts the
    winner exactly where the transaction is open, the writes are uncommitted and
    the claim must still be held.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    original = AsyncSession.commit
    pinned, release = asyncio.Event(), asyncio.Event()
    state = {"armed": True}

    async def commit(self):
        if state["armed"]:
            state["armed"] = False
            pinned.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return await original(self)

    AsyncSession.commit = commit
    try:
        yield pinned, release
    finally:
        AsyncSession.commit = original


async def test_a_contender_cannot_enter_while_the_winners_transaction_is_open(
        file_backed):
    """THE GUARANTEE, PINNED AT THE COMMIT BOUNDARY.

    The winner is held inside `db.commit()`: restoration has written its UPDATE
    and its INSERT, the SQLite write lock is held, and nothing is durable yet.
    A contender arriving here must be refused by the claim without touching the
    database — reaching the compare-and-set was measured blocking for the full
    driver busy timeout (5.01s) and then raising `database is locked`, which is
    HTTP 500 on the heartbeat endpoint and a stall for every other ATS write.

    This test FAILS if the claim is released before the commit completes.
    """
    client, probe, _engine = file_backed
    sess = await _silent_session(probe, scope=["src/**"], locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1

    async with _pin_the_first_commit() as (pinned, release):
        winner = asyncio.create_task(_heartbeat(client, sid))
        await asyncio.wait_for(pinned.wait(), timeout=15)

        started = asyncio.get_running_loop().time()
        contender = await _heartbeat(client, sid)
        elapsed = asyncio.get_running_loop().time() - started

        release.set()
        winner_resp = await asyncio.wait_for(winner, timeout=15)

    assert contender.status_code == 200, f"contention surfaced as an error: {contender.text}"
    assert contender.json()["restoration_outcome"] == "concurrent", contender.json()
    assert contender.json()["locks_restored"] == []
    assert elapsed < 1.0, (
        f"the contender waited {elapsed:.2f}s — it reached the database write lock")

    assert winner_resp.status_code == 200, winner_resp.text
    assert winner_resp.json()["locks_restored"] == ["src/**"]

    rows = (await probe.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all()
    assert len(rows) == 1 and rows[0].mode == "exclusive"
    assert not rows[0].authority_bearing
    row = await _row(probe, sid)
    assert row.reaped_locks == ""
    assert "[resurrected: 1 lock(s) restored]" in (row.summary or ""), row.summary


async def test_an_unrelated_session_is_not_stalled_by_a_restoration_contender(
        file_backed):
    """A contender must not queue behind the winner and drag others with it."""
    client, probe, _engine = file_backed
    sess = await _silent_session(probe, scope=["src/**"], locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1

    async with _pin_the_first_commit() as (pinned, release):
        winner = asyncio.create_task(_heartbeat(client, sid))
        await asyncio.wait_for(pinned.wait(), timeout=15)

        started = asyncio.get_running_loop().time()
        contenders = await asyncio.gather(*(_heartbeat(client, sid) for _ in range(3)))
        elapsed = asyncio.get_running_loop().time() - started

        release.set()
        await asyncio.wait_for(winner, timeout=15)

    assert {r.status_code for r in contenders} == {200}, [r.text for r in contenders]
    assert all(r.json()["restoration_outcome"] == "concurrent" for r in contenders)
    assert elapsed < 1.0, f"three contenders took {elapsed:.2f}s"

    rows = (await probe.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all()
    assert len(rows) == 1


async def test_a_failed_restoration_releases_the_claim_instead_of_deadlocking(
        file_backed):
    """The claim is released in a `finally`, so an exception between acquire and
    commit must not strand it. A leaked claim would make every later heartbeat
    for that session answer `concurrent` forever — permanently unrestorable and
    permanently un-liveness-stamped."""
    from sqlalchemy.ext.asyncio import AsyncSession

    client, probe, _engine = file_backed
    sess = await _silent_session(probe, scope=["src/**"], locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1

    original = AsyncSession.commit
    state = {"armed": True}

    async def explode(self):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("the transaction blew up with the claim held")
        return await original(self)

    AsyncSession.commit = explode
    try:
        with pytest.raises(RuntimeError):
            await _heartbeat(client, sid)
    finally:
        AsyncSession.commit = original

    # The transaction rolled back, so nothing was granted and the journal stands.
    assert await _locks_of(probe, sid) == {}
    assert await _journal_of(probe, sid) != "", "a failed attempt ate the journal"
    # And the claim is free: the next attempt is not answered 'concurrent'.
    body = (await _heartbeat(client, sid)).json()
    assert body["restoration_outcome"] == "restored", body
    assert body["locks_restored"] == ["src/**"]


async def test_the_compare_and_set_refuses_a_stale_journal_read(file_backed):
    """The backstop, exercised without contention: a restorer whose in-memory
    journal no longer matches the row grants nothing. This is what keeps "at
    most one grant" true for a caller that bypasses the in-process claim — a
    second server process, say.

    Two SEPARATE sessions deliberately. Doing this in one ORM session cannot
    test anything: setting the attribute back marks the row dirty, autoflush
    writes the stale bytes to the database before the compare-and-set runs, and
    the CAS then matches its own write.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ai_team_sync.routers.sessions import restore_reaped_locks

    _client, probe, engine = file_backed
    sess = await _silent_session(probe, scope=["src/**"], locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as reader:
        stale_row = (await reader.execute(
            select(Session).where(Session.id == sid))).scalar_one()
        assert stale_row.reaped_locks, "precondition: this restorer read an armed journal"

        # Somebody else consumes the journal first, on another connection.
        async with factory() as other:
            winner = (await other.execute(
                select(Session).where(Session.id == sid))).scalar_one()
            winner.reaped_locks = ""
            await other.commit()

        outcome = await restore_reaped_locks(reader, stale_row, was_auto_completed=True)
        await reader.commit()

    assert outcome.outcome == "concurrent", outcome
    assert outcome.restored == []
    assert await _locks_of(probe, sid) == {}


async def test_a_cas_loser_cannot_flush_a_journal_consumption_it_did_not_win(
        file_backed):
    """A losing contender must leave NO dirty ORM state.

    The in-memory `reaped_locks` used to be cleared before the rowcount was
    checked, so a loser held a dirty object and any caller that committed
    afterwards flushed a journal consumption it had not won — the one write a
    loser must never perform. `heartbeat_session` rolls back, so nothing live
    depended on it; this asserts the property at the function, where the next
    caller will.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ai_team_sync.routers.sessions import restore_reaped_locks

    _client, probe, engine = file_backed
    sess = await _silent_session(probe, scope=["src/**"], locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as loser:
        stale_row = (await loser.execute(
            select(Session).where(Session.id == sid))).scalar_one()
        assert stale_row.reaped_locks, "precondition: this restorer read an armed journal"

        # Another connection REPLACES the journal — it does not empty it — so a
        # loser that flushes its own clear is visible as data loss.
        replacement = json.dumps({"repo_root": ROOT, "locks": [
            {"pattern": "other/**", "mode": "advisory", "reason": "the winner's",
             "expires_at": (_utcnow() + timedelta(hours=2)).isoformat(),
             "created_at": _long_ago().isoformat()}]})
        async with factory() as other:
            row = (await other.execute(
                select(Session).where(Session.id == sid))).scalar_one()
            row.reaped_locks = replacement
            await other.commit()

        outcome = await restore_reaped_locks(loser, stale_row, was_auto_completed=True)
        await loser.commit()          # the commit a loser must make harmless

    assert outcome.outcome == "concurrent", outcome
    assert outcome.restored == []
    assert await _locks_of(probe, sid) == {}
    surviving = await _journal_of(probe, sid)
    assert surviving == replacement, (
        "the CAS loser flushed a journal consumption it did not win: "
        f"{surviving!r}")


async def test_the_winner_leaves_its_in_memory_journal_consistent_with_the_row(
        file_backed):
    """The other half: the winner's object must agree with the row it wrote, so
    anything reading it later in the same request sees the consumed journal."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ai_team_sync.routers.sessions import restore_reaped_locks

    _client, probe, engine = file_backed
    sess = await _silent_session(probe, scope=["src/**"], locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as winner:
        row = (await winner.execute(
            select(Session).where(Session.id == sid))).scalar_one()
        outcome = await restore_reaped_locks(winner, row, was_auto_completed=True)
        assert outcome.outcome == "restored", outcome
        assert row.reaped_locks == "", (
            "the winner's in-memory journal still looks armed after consuming it")
        await winner.commit()

    assert await _journal_of(probe, sid) == ""


async def test_concurrent_restoration_still_loses_to_a_newer_holder(file_backed):
    client, probe, _engine = file_backed
    sess = await _silent_session(probe, scope=["src/**", "docs/**"],
                                 locks=[("src/**", "exclusive"), ("docs/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(probe) == 1
    newcomer_id = await _newcomer_holding(probe, "src/**")

    results = await asyncio.gather(*(_heartbeat(client, sid) for _ in range(3)))
    assert {r.status_code for r in results} == {200}

    assert await _locks_of(probe, newcomer_id) == {"src/**": "exclusive"}
    mine = (await probe.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all()
    assert [l.pattern for l in mine] == ["docs/**"], [l.pattern for l in mine]
    holders = (await probe.execute(
        select(ScopeLock).where(ScopeLock.pattern == "src/**"))).scalars().all()
    assert len(holders) == 1 and holders[0].session_id == newcomer_id


# ── ONE AUTHORITY DOOR, and a refused lane that stops reading as held ────
#
# Operator rulings 2026-09-16. Ruling 1: session scope and POST /api/locks must
# share ONE pattern contract, rather than the session door quietly keeping its
# own. Ruling 2: after a resurrection that could not give a lane back, every
# authority-enforcement surface must agree with the LOCKS TABLE, not with the
# `scope` string that still names the lane.

async def _create_session(client, **over):
    body = {"developer": "pvestal", "agent": "claude-code:owner", "repo_root": ROOT,
            "description": "door test", "auto_lock": True, "lock_mode": "exclusive"}
    body.update(over)
    return await client.post("/api/sessions", json=body)


async def test_a_trailing_space_cannot_broaden_a_lane_across_resurrection(client, db_session):
    """THE #2760 CLAIM-1 REPRO, as an assertion.

    'src/** ' used to be stored verbatim by the session door, journalled with
    its trailing space, and handed back by restoration as 'src/**' — which
    covers src/a.py where the original did not. Authority silently widened by
    going away and coming back.
    """
    r = await _create_session(client, scope=["src/** "])
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    before = await _locks_of(db_session, sid)
    for lk in (await db_session.execute(
            select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all():
        lk.created_at = _long_ago()
    sess = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    sess.started_at = sess.last_heartbeat = _long_ago()
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1
    assert (await client.post(f"/api/sessions/{sid}/heartbeat")).status_code == 200

    db_session.expire_all()
    assert await _locks_of(db_session, sid) == before, "authority changed across resurrection"


@pytest.mark.parametrize("pattern,valid", [
    ("src/**", True),
    ("src/a.py", True),
    ("packages/scene_generation/builder.py", True),
    ("src/** ", True),            # normalised by both doors, not rejected
    ("the builder module", False),  # prose: rejected by both doors
    ("", False),
])
async def test_both_doors_adjudicate_a_pattern_identically(client, db_session, pattern, valid):
    """Ruling 1's whole point: ONE contract, not two that happen to agree today.

    The session door is asserted against the LOCK door's own answer for the
    same string, so the two cannot drift apart without this failing.
    """
    # ANCHORED TO DIFFERENT REPOS ON PURPOSE. Both doors are asked about the
    # same STRING; if they shared a repo the lock door's own lock would make the
    # session door answer 409 and the comparison would measure conflict
    # detection instead of the pattern contract.
    anchor = await _create_session(client, scope=[], auto_lock=False,
                                   repo_root="/srv/door-lock")
    assert anchor.status_code == 201
    lock_r = await client.post("/api/locks", json={
        "session_id": anchor.json()["id"], "pattern": pattern,
        "mode": "advisory", "reason": "door parity"})
    lock_ok = lock_r.status_code < 400

    sess_r = await _create_session(client, scope=[pattern], agent="claude-code:other",
                                   repo_root="/srv/door-session")
    sess_ok = sess_r.status_code < 400

    assert lock_ok == valid, f"lock door disagreed on {pattern!r}: {lock_r.status_code}"
    assert sess_ok == lock_ok, (
        f"doors disagree on {pattern!r}: lock={lock_r.status_code} session={sess_r.status_code}")
    if valid:
        # and they agree on the STORED form, not merely on admitting it
        assert sess_r.json()["scope"] == [lock_r.json()["pattern"]]


async def test_a_well_formed_scope_pattern_is_stored_unchanged(client, db_session):
    """The repair must not quietly rewrite claims that were always fine."""
    patterns = ["src/**", "src/ai_team_sync/routers/sessions.py", "tests/**", "*.md"]
    r = await _create_session(client, scope=patterns)
    assert r.status_code == 201, r.text
    assert r.json()["scope"] == patterns
    assert set((await _locks_of(db_session, r.json()["id"])).keys()) == set(patterns)


async def test_an_invalid_pattern_leaves_no_session_and_no_lock_behind(client, db_session):
    """Rejection BEFORE authority exists, not a cleanup afterwards.

    Validating inside the lock loop would refuse the second pattern with the
    session row and the first lock already written.
    """
    before_sessions = len((await db_session.execute(select(Session))).scalars().all())
    before_locks = len((await db_session.execute(select(ScopeLock))).scalars().all())

    r = await _create_session(client, scope=["src/**", "and also the docs please"])
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_scope_pattern"

    db_session.expire_all()
    assert len((await db_session.execute(select(Session))).scalars().all()) == before_sessions
    assert len((await db_session.execute(select(ScopeLock))).scalars().all()) == before_locks


async def test_a_lane_that_was_not_restored_stops_counting_as_a_claim(client, db_session):
    """END-TO-END NEGATIVE (#2760 item 3).

    A is reaped; B takes A's lane exclusively; A comes back. Restoration
    correctly REFUSES the lane — and from that moment the guard, the board and
    the locks table must all agree that A does not hold it, even though A's
    `scope` still says 'src/**'.
    """
    from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check

    a = await _silent_session(db_session, agent="claude-code:aaaaaaaa",
                              scope=["src/**"], locks=[("src/**", "exclusive")])
    assert await auto_complete_stale_sessions(db_session) == 1

    b = await _create_session(client, scope=["src/**"], agent="claude-code:bbbbbbbb")
    assert b.status_code == 201, b.text

    revived = await client.post(f"/api/sessions/{a.id}/heartbeat")
    assert revived.status_code == 200
    assert revived.json()["locks_not_restored"] == ["src/**"], revived.json()
    assert revived.json()["lock_count"] == 0

    # The board, on a LATER read that carries no restoration outcome of its own.
    listing = (await client.get("/api/sessions", params={"status": "active"})).json()
    mine = next(s for s in listing if s["id"] == a.id)
    assert mine["scope"] == ["src/**"], "intended scope stays visible as metadata"
    assert mine["lock_count"] == 0
    assert mine["locks_not_restored"] == ["src/**"], "the board forgot the lane was lost"

    # The enforcement surface, fed exactly what the board serves.
    locks = (await client.get("/api/locks")).json()
    locks = locks if isinstance(locks, list) else locks.get("locks", [])
    ok, why = claim_check("src/a.py", ROOT, a.id, "", listing, locks)
    assert ok is False, "a refused lane still passed the authority check"
    assert "not restored" in why.lower(), why


async def test_a_relane_taken_again_outranks_the_record_of_its_absence(client, db_session):
    """The read-side subtraction: re-taking a lane needs no second write."""
    from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check

    a = await _silent_session(db_session, agent="claude-code:aaaaaaaa",
                              scope=["src/**"], locks=[("src/**", "exclusive")])
    b = await _create_session(client, scope=["src/**"], agent="claude-code:bbbbbbbb",
                              auto_lock=False)
    assert await auto_complete_stale_sessions(db_session) == 1
    bl = await client.post("/api/locks", json={
        "session_id": b.json()["id"], "pattern": "src/**",
        "mode": "exclusive", "reason": "took the lane"})
    assert bl.status_code < 400, bl.text
    assert (await client.post(f"/api/sessions/{a.id}/heartbeat")).status_code == 200

    # B departs and A re-takes the lane the ordinary way.
    # delete_lock is owner-bound: a LIVE session's lane is nobody else's to drop,
    # so the actor has to name itself (403 otherwise — and an unasserted 403 here
    # is what made the first version of this test look like a product defect).
    dropped = await client.delete(f"/api/locks/{bl.json()['id']}",
                                  params={"actor_session_id": b.json()["id"]})
    assert dropped.status_code == 204, dropped.text
    retaken = await client.post("/api/locks", json={
        "session_id": a.id, "pattern": "src/**", "mode": "exclusive", "reason": "re-taken"})
    assert retaken.status_code < 400, retaken.text

    listing = (await client.get("/api/sessions", params={"status": "active"})).json()
    mine = next(s for s in listing if s["id"] == a.id)
    assert mine["locks_not_restored"] == [], "a held lock must outrank the absence record"
    locks = (await client.get("/api/locks")).json()
    locks = locks if isinstance(locks, list) else locks.get("locks", [])
    ok, _ = claim_check("src/a.py", ROOT, a.id, "", listing, locks)
    assert ok is True


# ── locks_not_restored IS AUTHORITY STATE, not a record of the last event ──
#
# Operator ruling 2026-09-17. THE INVARIANT: once ATS knows a session does not
# hold an intended lane, that lane stays not-restored until ATS can prove the
# session holds a LIVE lock covering it. A later lifecycle event that
# adjudicates nothing must not erase the known loss.

from ai_team_sync.routers.sessions import (  # noqa: E402
    NOT_RESTORED_NEWER_HOLDER,
    OUTCOME_NOTHING_JOURNALLED,
    OUTCOME_RESTORED,
    REFUSAL_ANCHOR_MOVED,
    REFUSAL_INVALID_JOURNAL,
    REFUSAL_UNIDENTIFIED_OWNER,
)


async def _age_session(db, sid):
    sess = (await db.execute(select(Session).where(Session.id == sid))).scalar_one()
    sess.started_at = sess.last_heartbeat = _long_ago()
    for lk in (await db.execute(
            select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all():
        lk.created_at = _long_ago()
    await db.commit()


async def _board(client, sid):
    listing = (await client.get("/api/sessions", params={"status": "active"})).json()
    return next(s for s in listing if s["id"] == sid), listing


async def _guard(client, listing, sid, rel="src/a.py"):
    from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check
    locks = (await client.get("/api/locks")).json()
    locks = locks if isinstance(locks, list) else locks.get("locks", [])
    return claim_check(rel, ROOT, sid, "", listing, locks)


async def test_a_second_empty_resurrection_cannot_erase_an_unresolved_loss(client, db_session):
    """THE REPRODUCED REGRESSION, exactly as ruled.

    A loses a lane, B releases it, A is reaped and resurrected again with an
    EMPTY journal — and must not get the lane back merely because another
    lifecycle cycle happened. Nothing in this sequence is a live lock for A.
    """
    a = await _create_session(client, scope=["src/**"], agent="claude-code:aaaaaaaa")
    aid = a.json()["id"]
    await _age_session(db_session, aid)
    assert await auto_complete_stale_sessions(db_session) == 1

    b = await _create_session(client, scope=["src/**"], agent="claude-code:bbbbbbbb")
    bid = b.json()["id"]

    first = await client.post(f"/api/sessions/{aid}/heartbeat")
    assert first.json()["locks_not_restored"] == ["src/**"]

    await client.post(f"/api/sessions/{bid}/complete", json={"summary": "B leaves"})

    # A is reaped again holding NOTHING, so the reaper journals an empty list.
    await _age_session(db_session, aid)
    assert await auto_complete_stale_sessions(db_session) == 1
    second = await client.post(f"/api/sessions/{aid}/heartbeat")
    assert second.json()["restoration_outcome"] == OUTCOME_NOTHING_JOURNALLED

    mine, listing = await _board(client, aid)
    assert mine["lock_count"] == 0
    assert mine["locks_not_restored"] == ["src/**"], "a lifecycle cycle erased a live loss"
    ok, why = await _guard(client, listing, aid)
    assert ok is False, "A regained authority merely because another cycle occurred"
    assert "not restored" in why.lower()


async def test_repeated_cycles_cannot_wear_the_loss_away(client, db_session):
    """Not one extra cycle — several. The state must be a fixed point."""
    a = await _create_session(client, scope=["src/**"], agent="claude-code:aaaaaaaa")
    aid = a.json()["id"]
    await _age_session(db_session, aid)
    assert await auto_complete_stale_sessions(db_session) == 1
    b = await _create_session(client, scope=["src/**"], agent="claude-code:bbbbbbbb")
    assert (await client.post(f"/api/sessions/{aid}/heartbeat")).status_code == 200
    await client.post(f"/api/sessions/{b.json()['id']}/complete", json={"summary": "gone"})

    for cycle in range(3):
        await _age_session(db_session, aid)
        assert await auto_complete_stale_sessions(db_session) == 1
        assert (await client.post(f"/api/sessions/{aid}/heartbeat")).status_code == 200
        mine, listing = await _board(client, aid)
        assert mine["locks_not_restored"] == ["src/**"], f"loss erased on cycle {cycle}"
        ok, _ = await _guard(client, listing, aid)
        assert ok is False, f"authority returned on cycle {cycle}"


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: setattr(s, "repo_root", "/srv/moved-away"), REFUSAL_ANCHOR_MOVED),
    (lambda s: setattr(s, "creator_uid", None), REFUSAL_UNIDENTIFIED_OWNER),
])
async def test_a_whole_journal_refusal_records_the_lanes_it_leaves_unheld(
        client, db_session, mutate, reason):
    """A refusal is where ATS LEARNS the session holds nothing — not a blank.

    Refusing the whole journal used to report no lanes at all, so the session
    came back active with its scope intact and nothing recording the loss.
    """
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "exclusive")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1

    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    mutate(row)
    await db_session.commit()

    revived = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert revived.status_code == 200, revived.text
    assert revived.json()["restoration_reason"] == reason
    assert revived.json()["lock_count"] == 0
    assert revived.json()["locks_not_restored"] == ["src/**"], revived.json()


async def test_an_unreadable_journal_records_nothing_but_erases_nothing(client, db_session):
    """When the lanes are genuinely unknowable, say nothing new — and keep what
    was already known. Unknown must never read as 'held'."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["src/**", NOT_RESTORED_NEWER_HOLDER]])
    row.reaped_locks = "{not json at all"
    row.status, row.auto_completed = "completed", True
    await db_session.commit()

    revived = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert revived.status_code == 200
    assert revived.json()["locks_not_restored"] == ["src/**"], "unreadable journal erased a loss"


async def test_an_expired_advisory_lock_cannot_clear_a_loss(client, db_session):
    """A dead row is not authority. GET /api/locks does not show it and it
    blocks nobody, so it must not be accepted as proof the lane came back."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["src/**", NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()
    db_session.add(ScopeLock(session_id=sid, pattern="src/**", mode="advisory",
                             reason="2760", created_at=_long_ago(),
                             expires_at=_utcnow() - timedelta(minutes=5)))
    await db_session.commit()

    mine, listing = await _board(client, sid)
    assert mine["locks_not_restored"] == ["src/**"], "an expired advisory row cleared the loss"
    ok, _ = await _guard(client, listing, sid)
    assert ok is False


async def test_a_genuinely_live_lock_does_clear_the_loss(client, db_session):
    """The one transition that IS allowed to turn 'not held' back into 'held'."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["src/**", NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()
    db_session.add(ScopeLock(session_id=sid, pattern="src/**", mode="advisory",
                             reason="2760", created_at=_utcnow(),
                             expires_at=_utcnow() + timedelta(hours=2)))
    await db_session.commit()

    mine, listing = await _board(client, sid)
    assert mine["locks_not_restored"] == [], "a live lock did not clear the loss"
    ok, _ = await _guard(client, listing, sid)
    assert ok is True


async def test_respelling_scope_cannot_walk_past_a_lost_lane(client, db_session):
    """The loss is about FILES, not about the spelling of a scope string."""
    sess = await _silent_session(db_session, scope=["src/**"])
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["src/**", NOT_RESTORED_NEWER_HOLDER]])
    row.scope = json.dumps(["src//**"])
    await db_session.commit()

    _, listing = await _board(client, sid)
    ok, why = await _guard(client, listing, sid)
    assert ok is False, "a respelled scope entry walked past its own recorded loss"


# ── PATCH is the same authority door as creation ─────────────────────────

async def test_patch_normalises_scope_through_the_lock_contract(client, db_session):
    """extend_scope writes the merged scope back through PATCH, so PATCH must
    not be able to persist a spelling the lock door would not."""
    r = await _create_session(client, scope=[], auto_lock=False)
    sid = r.json()["id"]
    patched = await client.patch(f"/api/sessions/{sid}", json={"scope": ["src/** "]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["scope"] == ["src/**"], "PATCH persisted an unnormalised spelling"


async def test_patch_refuses_prose_and_changes_nothing(client, db_session):
    """Atomicity: a refused PATCH leaves scope, description and status alone."""
    r = await _create_session(client, scope=["src/**"], description="before")
    sid = r.json()["id"]
    before_locks = await _locks_of(db_session, sid)

    bad = await client.patch(f"/api/sessions/{sid}", json={
        "scope": ["src/**", "and the docs too"], "description": "after",
        "summary": "should not land"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["detail"]["error"] == "invalid_scope_pattern"

    db_session.expire_all()
    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["scope"] == ["src/**"]
    assert after["description"] == "before", "a refused PATCH still mutated the session"
    assert after["summary"] in (None, ""), "a refused PATCH still wrote a summary"
    assert await _locks_of(db_session, sid) == before_locks


# ── negative state is HISTORY; a live lock only SUPPRESSES it ────────────
#
# Operator ruling 2026-09-17, repairs 1 and 2. An unresolved loss stays
# persisted. A live lock hides it at read/guard time. When that lock expires or
# disappears the loss becomes visible again by itself — nothing has to remember
# to re-record it.

async def test_a_restored_lock_expiring_makes_the_old_loss_visible_again(client, db_session):
    """THE TRANSITION THE SUITE MISSED (review BLOCK 1).

    known loss -> lane re-taken -> reap -> SUCCESSFUL restoration -> the restored
    lock carries its ORIGINAL expiry (restoration never mints a fresh TTL) and
    dies -> the loss must reappear, and `scope` alone must not grant.
    """
    a = await _create_session(client, scope=["src/**"], auto_lock=False,
                              agent="claude-code:aaaaaaaa")
    aid = a.json()["id"]
    row = (await db_session.execute(select(Session).where(Session.id == aid))).scalar_one()
    row.locks_not_restored = json.dumps([["src/**", NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()

    # The lane is genuinely re-taken, so the loss is suppressed — not erased.
    db_session.add(ScopeLock(session_id=aid, pattern="src/**", mode="advisory",
                             reason="re-taken", created_at=_utcnow(),
                             expires_at=_utcnow() + timedelta(minutes=30)))
    await db_session.commit()
    mine, _ = await _board(client, aid)
    assert mine["locks_not_restored"] == [], "a live lock must suppress the loss"

    # Reaped and successfully resurrected: the journal gives the lane back.
    await _age_session(db_session, aid)
    assert await auto_complete_stale_sessions(db_session) == 1
    hb = await client.post(f"/api/sessions/{aid}/heartbeat")
    assert hb.json()["restoration_outcome"] == OUTCOME_RESTORED
    assert hb.json()["locks_restored"] == ["src/**"]

    # The restored lock keeps its ORIGINAL expiry. Let it elapse.
    db_session.expire_all()
    for lk in (await db_session.execute(
            select(ScopeLock).where(ScopeLock.session_id == aid))).scalars().all():
        lk.expires_at = _utcnow() - timedelta(minutes=1)
    await db_session.commit()

    mine, listing = await _board(client, aid)
    # The point of the transition: the proof is GONE, not merely stale.
    locks = (await client.get("/api/locks")).json()
    locks = locks if isinstance(locks, list) else locks.get("locks", [])
    assert [lk for lk in locks if lk.get("session_id") == aid] == [], \
        "precondition: the restored lock must no longer be live"
    assert mine["locks_not_restored"] == ["src/**"], \
        "a successful restoration discarded the loss, so its expiry handed scope back"
    ok, why = await _guard(client, listing, aid)
    assert ok is False, "scope granted with no live lock once the restored lock expired"
    assert "not restored" in why.lower()


@pytest.mark.parametrize("entry,label", [
    ({"pattern": "docs/my file.md", "mode": "advisory", "reason": "legacy",
      "expires_at": None, "created_at": None}, "legacy pattern the contract now rejects"),
    ({"pattern": "src/**", "mode": "sideways", "reason": "x",
      "expires_at": None, "created_at": None}, "unknown mode"),
    ({"pattern": "src/**", "mode": "advisory", "reason": 17,
      "expires_at": None, "created_at": None}, "reason is not text"),
    ({"pattern": "src/**", "mode": "advisory", "reason": "x",
      "expires_at": "not-a-time", "created_at": None}, "unreadable expiry"),
])
async def test_every_post_cas_refusal_preserves_the_lanes_it_abandons(
        client, db_session, entry, label):
    """CLASS INVARIANT, not one example (review BLOCK 2).

    The CAS has already emptied the journal by the time _grant_from_journal
    runs, so `raw` is the last copy of what the session held. EVERY refusal
    there abandons restoration for good and must leave the lanes recorded —
    otherwise the session returns active, holding nothing, with `scope` still
    naming the lane and nothing to contradict it.
    """
    pattern = entry["pattern"] if isinstance(entry, dict) else "src/**"
    if isinstance(entry, dict):
        entry = dict(entry)
        entry["expires_at"] = entry["expires_at"] or (_utcnow() + timedelta(hours=1)).isoformat()
        entry["created_at"] = entry["created_at"] or _long_ago().isoformat()

    sess = await _silent_session(db_session, scope=[pattern], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.status, row.auto_completed = "completed", True
    row.reaped_locks = json.dumps({"repo_root": ROOT, "locks": [entry]})
    await db_session.commit()

    revived = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert revived.status_code == 200, revived.text
    assert revived.json()["restoration_reason"] == REFUSAL_INVALID_JOURNAL, label
    assert revived.json()["lock_count"] == 0
    assert revived.json()["locks_not_restored"] == [pattern], \
        f"{label}: refusal abandoned the lane without recording it"

    # And the session cannot then take authority from scope alone.
    _, listing = await _board(client, sid)
    ok, _why = await _guard(client, listing, sid, rel=pattern.replace("**", "a.py"))
    assert ok is False, f"{label}: scope granted after a post-CAS refusal"


async def test_an_unnameable_entry_still_preserves_the_lanes_that_ARE_nameable(
        client, db_session):
    """A journal is refused WHOLE, but the refusal is not all-or-nothing about
    what it records.

    A non-object entry has no pattern to read, so that lane is genuinely
    unnameable — the ruling records lanes only where they can be identified
    safely. Its siblings can still be named, and losing them too would punish
    the session for the one entry nobody can read.
    """
    sess = await _silent_session(db_session, scope=["src/**"], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.status, row.auto_completed = "completed", True
    row.reaped_locks = json.dumps({"repo_root": ROOT, "locks": [
        {"pattern": "src/**", "mode": "advisory", "reason": "nameable",
         "expires_at": (_utcnow() + timedelta(hours=1)).isoformat(),
         "created_at": _long_ago().isoformat()},
        "not-an-object",
    ]})
    await db_session.commit()

    revived = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert revived.status_code == 200
    assert revived.json()["restoration_reason"] == REFUSAL_INVALID_JOURNAL
    assert revived.json()["lock_count"] == 0
    assert revived.json()["locks_not_restored"] == ["src/**"], \
        "the nameable sibling was discarded along with the unreadable entry"

    _, listing = await _board(client, sid)
    ok, _why = await _guard(client, listing, sid)
    assert ok is False


async def test_the_journal_is_consumed_before_any_refusal_can_run(client, db_session):
    """Why the invariant above is needed: the column really is empty by then."""
    sess = await _silent_session(db_session, scope=["src/**"], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.status, row.auto_completed = "completed", True
    row.reaped_locks = json.dumps(
        {"repo_root": ROOT, "locks": [{"pattern": "src/**", "mode": "nonsense",
                                       "reason": "x",
                                       "expires_at": (_utcnow() + timedelta(hours=1)).isoformat(),
                                       "created_at": _long_ago().isoformat()}]})
    await db_session.commit()

    assert (await client.post(f"/api/sessions/{sid}/heartbeat")).status_code == 200
    db_session.expire_all()
    left = (await db_session.execute(
        select(Session.reaped_locks).where(Session.id == sid))).scalar_one()
    assert not left, "journal survived a refusal; the invariant's premise is wrong"


async def test_an_equivalent_spelling_of_a_live_lock_suppresses_the_loss(client, db_session):
    """A lock and a loss are two spellings of ONE question.

    Raw string equality let a live 'src//**' fail to suppress a 'src/**' loss
    although both canonicalise to the same coverage: claim_check granted (it
    consults live locks first) while the board went on telling the owner the
    lane was unheld and had to be re-taken — the board contradicting the
    enforcement surface about the same lane.
    """
    sess = await _silent_session(db_session, scope=["src/**"], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["src/**", NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()
    db_session.add(ScopeLock(session_id=sid, pattern="src//**", mode="advisory",
                             reason="re-taken, spelled differently", created_at=_utcnow(),
                             expires_at=_utcnow() + timedelta(hours=2)))
    await db_session.commit()

    mine, listing = await _board(client, sid)
    assert mine["locks_not_restored"] == [], \
        "a live lock with an equivalent spelling did not suppress the loss"
    ok, _why = await _guard(client, listing, sid)
    assert ok is True, "enforcement must be unchanged — the live lock always granted"


@pytest.mark.parametrize("lock_pattern,loss,suppressed", [
    ("src/**", "src/**", True),        # the coverage floor: exact equality still works
    ("src//**", "src/**", True),       # equivalent spelling now suppresses
    ("src/./**", "src/**", True),
    ("src/*.py", "src/a*", False),     # DIFFERENT globs stay distinct (#2806's question)
    ("docs/**", "src/**", False),      # unrelated lanes unaffected
])
async def test_these_relative_spelling_pairs_suppress_only_when_canonically_equal(
        client, db_session, lock_pattern, loss, suppressed):
    """FIVE NAMED RELATIVE PAIRS. Not a general proof, and the name no longer
    claims to be one.

    What these cases establish, and only this: for these particular RELATIVE
    spellings, a live lock suppresses a loss exactly when the two are canonically
    equal — exact equality still suppresses (the floor), '//' and '/./' now do
    too, and two patterns that are merely similar ('src/*.py' vs 'src/a*') or
    unrelated ('docs/**' vs 'src/**') do not.

    They say NOTHING about absolute-versus-relative spellings, about reader roots
    other than the session's, or about coverage between different globs. An
    earlier name asserted that canonical subtraction "widens string equality
    without inventing coverage" in general; five hand-picked relative pairs
    cannot carry that, and while it read as proof the delta shipped an
    enforcement change at froot='' that this test could not see.

    The general property is evidenced where it is actually tested:
    test_an_absolute_lock_does_not_collapse_into_a_relative_loss and
    test_the_reporting_repair_cannot_remove_a_refusal_the_guard_would_not_replace
    pin the seam, each discriminated by a mutation restoring the root argument.
    """
    sess = await _silent_session(db_session, scope=[loss], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([[loss, NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()
    db_session.add(ScopeLock(session_id=sid, pattern=lock_pattern, mode="advisory",
                             reason="held", created_at=_utcnow(),
                             expires_at=_utcnow() + timedelta(hours=2)))
    await db_session.commit()

    mine, _ = await _board(client, sid)
    assert (mine["locks_not_restored"] == []) is suppressed, \
        f"lock {lock_pattern!r} vs loss {loss!r}: expected suppressed={suppressed}"


async def test_an_absolute_lock_does_not_collapse_into_a_relative_loss(client, db_session):
    """Canonical subtraction must not depend on which root a reader holds.

    Canonicalising against the SESSION's repo_root relativised an absolute lock
    pattern, so a live '/repo/a.py' merged with a lost 'a.py' and suppressed it.
    Two spellings that only coincide under one particular root are NOT the same
    claim, and treating them as one moved enforcement (see the froot='' test).
    """
    sess = await _silent_session(db_session, scope=["a.py"], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["a.py", NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()
    db_session.add(ScopeLock(session_id=sid, pattern=f"{ROOT}/a.py", mode="advisory",
                             reason="absolute spelling", created_at=_utcnow(),
                             expires_at=_utcnow() + timedelta(hours=2)))
    await db_session.commit()

    mine, _ = await _board(client, sid)
    assert mine["locks_not_restored"] == ["a.py"], \
        "an absolute lock collapsed into a relative loss under the session root"


async def test_the_reporting_repair_cannot_remove_a_refusal_the_guard_would_not_replace(
        client, db_session):
    """THE ENFORCEMENT-NEUTRALITY SEAM (review BLOCK on the bounded delta).

    The server subtracts losses using patterns alone; the guard places patterns
    against the FILE's root, which can be '' when no repository is discovered or
    ATS_COORDINATED_REPOS names '/'. If the two disagree, suppressing a loss
    removes a refusal that the live-lock branch does NOT replace with a grant —
    a reporting change that silently moves enforcement.

    The control is the third assertion: with neither the loss nor the scope, the
    live lock ALONE grants nothing here, so the refusal is load-bearing.
    """
    from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check

    sess = await _silent_session(db_session, scope=["a.py"], agent="claude-code:aaaaaaaa")
    sid = sess.id
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    row.locks_not_restored = json.dumps([["a.py", NOT_RESTORED_NEWER_HOLDER]])
    await db_session.commit()
    db_session.add(ScopeLock(session_id=sid, pattern=f"{ROOT}/a.py", mode="advisory",
                             reason="absolute spelling", created_at=_utcnow(),
                             expires_at=_utcnow() + timedelta(hours=2)))
    await db_session.commit()

    listing = (await client.get("/api/sessions", params={"status": "active"})).json()
    locks = (await client.get("/api/locks")).json()
    locks = locks if isinstance(locks, list) else locks.get("locks", [])

    # The guard, run with NO file root — the seam the BLOCK was found at.
    ok, why = claim_check("a.py", "", sid, "", listing, locks)
    assert ok is False, "the reporting repair removed a refusal at froot=''"
    assert "not restored" in why.lower(), why

    # CONTROL: the live-lock branch alone grants nothing at this root, so the
    # refusal above is genuinely load-bearing rather than incidental.
    bare = [dict(s, scope=[], locks_not_restored=[]) for s in listing if s["id"] == sid]
    ok_lock_only, _ = claim_check("a.py", "", sid, "", bare, locks)
    assert ok_lock_only is False, \
        "control invalid: the live-lock branch grants here, so the test proves nothing"
