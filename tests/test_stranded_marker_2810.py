"""#2810 -- the STRANDED marker carries a COUNT, never a filename.

THE DEFECT. _LIFECYCLE_MARKER_RE ends a marker at its first ']'. The reaper used
to interpolate every stranded filename into that bracketed text, so a stranded
path containing ']' (src/a]tail.py) wrote "[STRANDED ... src/a]tail.py]", the
regex consumed only through "src/a]", and the next rewrite left "tail.py]"
grafted onto the operator's summary. #2445's one-marker de-duplication broke
with it.

THE CLASS FIX, the same one #2760 made for lock patterns: narrative text is the
wrong container for arbitrary syntax, so the marker says how many files were
stranded and the names travel as a list on the session.auto_completed event.

LEGACY ROWS. Summaries written before this fix still hold the filename-bearing
marker, so the parser still recognises it -- including one whose filenames
contain ']' -- and replaces it whole.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ai_team_sync import background_tasks
from ai_team_sync.background_tasks import auto_complete_stale_sessions, replace_lifecycle_marker
from ai_team_sync.config import settings
from ai_team_sync.models import Session

OWNER_TOKEN = "test-owner-capability-2810"
NARRATIVE = "operator handoff note"
BRACKETED = "src/a]tail.py"


def _long_ago() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=settings.session_inactivity_hours + 24)


def _repo_with_dirty(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("dirty\n")
    return repo


async def _silent_session(db, repo, *, summary=NARRATIVE) -> Session:
    """An identified, silent session anchored on `repo` with scope src/**, so a
    heartbeat after the reap is a legitimate resurrection."""
    sess = Session(developer="pvestal", agent="claude-code:owner", repo_root=str(repo),
                   scope=json.dumps(["src/**"]), status="active", summary=summary,
                   started_at=_long_ago(), last_heartbeat=_long_ago(),
                   creator_uid=os.getuid(),
                   approval_token_hash=hashlib.sha256(OWNER_TOKEN.encode()).hexdigest())
    db.add(sess)
    await db.commit()
    return sess


async def _row(db, session_id) -> Session:
    db.expire_all()
    return (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()


def _capture_events(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []

    async def fake_broadcast(session_id, event_type, data):
        seen.append((event_type, data))

    monkeypatch.setattr(background_tasks, "broadcast_event", fake_broadcast)
    return seen


# ── the ticket's repro, end to end: reap, then heartbeat ────────────────

async def test_a_bracket_in_a_stranded_path_leaves_no_fragment(client, db_session, tmp_path):
    repo = _repo_with_dirty(tmp_path, [BRACKETED, "src/ok.py", "docs/out.md"])
    sess = await _silent_session(db_session, repo)
    sid = sess.id

    assert await auto_complete_stale_sessions(db_session) == 1
    reaped = (await _row(db_session, sid)).summary or ""
    # #2554's contract survives: a reaped session still SAYS it stranded work.
    assert "[STRANDED 2 uncommitted file(s) in scope]" in reaped, reaped
    assert "tail.py" not in reaped and "src/ok.py" not in reaped, (
        f"a filename reached narrative text: {reaped!r}")

    resp = await client.post(f"/api/sessions/{sid}/heartbeat")
    assert resp.status_code == 200, resp.text
    summary = (await _row(db_session, sid)).summary or ""
    assert "tail.py" not in summary, f"fragment grafted onto the summary: {summary!r}"
    assert "STRANDED" not in summary, summary
    assert summary.count("[resurrected") == 1, summary
    # Exactly the narrative plus one marker: nothing grafted between them.
    assert summary.startswith(f"{NARRATIVE} [resurrected:"), summary
    assert summary.endswith("]") and summary.count("]") == 1, summary


async def test_the_stranded_names_live_on_the_auto_completed_event(
        db_session, tmp_path, monkeypatch):
    """The names are not lost, they move: a consumer reads a list, verbatim,
    instead of parsing prose."""
    seen = _capture_events(monkeypatch)
    repo = _repo_with_dirty(tmp_path, [BRACKETED, "src/ok.py", "docs/out.md"])
    sess = await _silent_session(db_session, repo)

    assert await auto_complete_stale_sessions(db_session) == 1
    (data,) = [d for t, d in seen if t == "session.auto_completed"]
    assert data["session_id"] == sess.id
    assert data["uncommitted_in_scope"] == [BRACKETED, "src/ok.py"]


async def test_the_count_marker_dedups_across_reap_cycles(db_session, tmp_path):
    repo = _repo_with_dirty(tmp_path, [BRACKETED])
    sess = await _silent_session(db_session, repo)
    for _ in range(3):
        row = await _row(db_session, sess.id)
        row.status, row.completed_at, row.last_heartbeat = "active", None, _long_ago()
        await db_session.commit()
        assert await auto_complete_stale_sessions(db_session) == 1

    summary = (await _row(db_session, sess.id)).summary or ""
    assert summary.count("[STRANDED") == 1 and summary.count("[auto-completed") == 1, summary
    assert "tail.py" not in summary, summary
    assert summary.startswith(NARRATIVE), summary


# ── the stored completion event is the DURABLE copy of the names ────────
# The websocket event is not stored. The v1 completion event in the outbox is,
# so the names are kept there, verbatim, under the payload's own sha256.

BRACKET_PAIR = "b[1].py"


async def _fresh_outbox_rows(db_engine, sid):
    """Read the outbox through a NEW database session: what was committed, not
    what this process still holds in memory."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from ai_team_sync.models import CompletionOutbox
    async with async_sessionmaker(db_engine, class_=AsyncSession)() as fresh:
        return (await fresh.execute(select(CompletionOutbox)
                                    .where(CompletionOutbox.session_id == sid))).scalars().all()


async def test_reaped_event_stores_the_exact_stranded_names(db_engine, db_session, tmp_path):
    from ai_team_sync import completion_events as ce
    repo = _repo_with_dirty(tmp_path, [BRACKETED, BRACKET_PAIR, "docs/out.md"])
    sess = await _silent_session(db_session, repo)
    sess.scope = json.dumps(["src/**", "*.py"])
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1
    (row,) = await _fresh_outbox_rows(db_engine, sess.id)
    assert row.completion_reason == "reaped"
    for name in (BRACKETED, BRACKET_PAIR):
        assert json.dumps(name) in row.payload, f"{name!r} not in the stored payload text"
    event = json.loads(row.payload)
    assert event["uncommitted_in_scope"] == [BRACKET_PAIR, BRACKETED]
    assert row.payload_sha256 == ce.sha256(event)
    # The field is additive: the id still derives from session + completed_at.
    assert event["event_id"] == ce.event_id_for(sess.id, event["completed_at"])


async def test_reaped_event_with_nothing_stranded_carries_an_empty_list(
        db_engine, db_session, tmp_path):
    repo = _repo_with_dirty(tmp_path, ["docs/out.md"])
    sess = await _silent_session(db_session, repo)
    assert await auto_complete_stale_sessions(db_session) == 1
    (row,) = await _fresh_outbox_rows(db_engine, sess.id)
    assert json.loads(row.payload)["uncommitted_in_scope"] == []


async def test_a_failed_scope_check_omits_the_field_rather_than_claim_none(
        db_engine, db_session, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(background_tasks, "uncommitted_for_scope", boom)
    sess = await _silent_session(db_session, _repo_with_dirty(tmp_path, [BRACKETED]))
    assert await auto_complete_stale_sessions(db_session) == 1
    (row,) = await _fresh_outbox_rows(db_engine, sess.id)
    assert "uncommitted_in_scope" not in json.loads(row.payload)


def test_owner_completed_and_default_events_carry_no_field():
    from ai_team_sync import completion_events as ce
    s = Session(id="s", developer="x", agent="a",
                completed_at=datetime(2026, 10, 9, 20, 0, 0, 1))
    owner = ce.build_event(s, None, ce.OWNER_COMPLETED, source={})
    assert "uncommitted_in_scope" not in owner
    with_field = ce.build_event(s, None, ce.REAPED, source={},
                                uncommitted_in_scope=[BRACKETED])
    assert with_field.pop("uncommitted_in_scope") == [BRACKETED]
    assert with_field == ce.build_event(s, None, ce.REAPED, source={})


async def test_owner_completion_outbox_event_has_no_field(client, db_engine):
    r = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": "claude-code:test", "scope": [],
        "repo_root": "/repo", "ticket_id": 2810})
    assert r.status_code == 201, r.text
    sid, token = r.json()["id"], r.headers["X-ATS-Approval-Token"]
    r = await client.patch(f"/api/sessions/{sid}", json={"status": "completed",
                                                         "summary": "done"},
                           headers={"X-ATS-Approval-Token": token})
    assert r.status_code == 200, r.text
    (row,) = await _fresh_outbox_rows(db_engine, sid)
    assert row.completion_reason == "owner_completed"
    assert "uncommitted_in_scope" not in json.loads(row.payload)


# ── legacy summaries written before #2810 ───────────────────────────────

LEGACY_AUTO = "[auto-completed: silent >20m (heartbeat lost)]"
NEW_NOTE = "[resurrected: 1 lock(s) restored]"


def test_a_legacy_marker_holding_a_bracket_is_replaced_whole():
    legacy = (f"{NARRATIVE} {LEGACY_AUTO} "
              f"[STRANDED 1 uncommitted file(s) in scope: {BRACKETED}]")
    assert replace_lifecycle_marker(legacy, NEW_NOTE) == f"{NARRATIVE} {NEW_NOTE}"


def test_a_legacy_marker_with_several_bracketed_names_is_replaced_whole():
    legacy = (f"{NARRATIVE} {LEGACY_AUTO} [STRANDED 3 uncommitted file(s) in scope: "
              "a]b.py, src/[x]y.py, src/c].py]")
    assert replace_lifecycle_marker(legacy, NEW_NOTE) == f"{NARRATIVE} {NEW_NOTE}"


def test_a_legacy_marker_followed_by_another_marker_stops_at_it():
    legacy = (f"{NARRATIVE} {LEGACY_AUTO} [STRANDED 1 uncommitted file(s) in scope: "
              f"{BRACKETED}] [resurrected: 0 lock(s) restored]")
    assert replace_lifecycle_marker(legacy, NEW_NOTE) == f"{NARRATIVE} {NEW_NOTE}"


def test_a_legacy_marker_never_swallows_narrative_written_after_it():
    """Bracket-awareness must not trade a grafted fragment for eaten prose: text
    after the marker that is not another marker is the operator's own."""
    tail = "then I added [this note] by hand"
    legacy = (f"{NARRATIVE} {LEGACY_AUTO} "
              f"[STRANDED 1 uncommitted file(s) in scope: src/plain.py] {tail}")
    assert replace_lifecycle_marker(legacy, NEW_NOTE) == f"{NARRATIVE} {tail} {NEW_NOTE}"


def test_a_legacy_marker_without_brackets_is_still_replaced():
    legacy = (f"{NARRATIVE} {LEGACY_AUTO} "
              "[STRANDED 2 uncommitted file(s) in scope: src/a.py, src/b.py]")
    assert replace_lifecycle_marker(legacy, NEW_NOTE) == f"{NARRATIVE} {NEW_NOTE}"


async def test_a_legacy_row_resurrects_without_a_fragment(client, db_session, tmp_path):
    """A row reaped by the old code, revived by the new code."""
    repo = _repo_with_dirty(tmp_path, [])
    sess = await _silent_session(db_session, repo)
    assert await auto_complete_stale_sessions(db_session) == 1
    row = await _row(db_session, sess.id)
    row.summary = (f"{NARRATIVE} {LEGACY_AUTO} "
                   f"[STRANDED 1 uncommitted file(s) in scope: {BRACKETED}]")
    await db_session.commit()

    resp = await client.post(f"/api/sessions/{sess.id}/heartbeat")
    assert resp.status_code == 200, resp.text
    summary = (await _row(db_session, sess.id)).summary or ""
    assert "tail.py" not in summary and "STRANDED" not in summary, summary
    assert summary.startswith(f"{NARRATIVE} [resurrected:"), summary
