"""A restart of a shared service is a recorded, queryable event (#2559).

RAISED BY THE OPERATOR 2026-08-24 after a live restart exposed the gap, and every
claim below was verified against the live ATS database before this was built:

  * team_status renders only `Decisions: N` -- a COUNT, never the content. A restart
    logged as a generic decision is therefore invisible to every other session.
    (mcp/server.py, the team_status branch.)
  * The one restart-shaped record in the live DB is a free-text `reasoning` blob
    holding "old PID 1969400 -> 2415302" in prose. Nothing can be queried, so
    "when was comfyui last bounced, and did it help" has no answer.
  * Concurrent restart is a LIVE condition, not a hypothesis: at the time of
    writing there were 5 active sessions, TWO of them anchored to /opt/anime-studio.
  * `systemctl restart` is completely unguarded -- the PreToolUse lock hook matches
    `Edit|Write|MultiEdit|NotebookEdit` only (hooks/pre_tool_use_lockcheck.py:28).

SCOPE. This is item 1 of the ticket (record + surface) only. It deliberately does
NOT refuse or gate restarts; the ticket's own warning is that "the failure mode to
avoid is a guard that makes an emergency recycle harder than doing it out-of-band."
Refusal (item 2) changes behaviour under an operator and is a separate decision.

WHY A NEW TABLE RATHER THAN REUSING ScopeLock -- measured, not assumed. A unit name
like 'comfyui' PASSES LockCreate's glob validator (no whitespace, short) but can
never fnmatch a real file path, so a service claim expressed as a ScopeLock would be
an lock that silently protects nothing. That is the exact failure the `reason`
column was added to fix in 2026-06, reintroduced in a form the validator cannot see.
"""

from __future__ import annotations

import pytest


async def _session(client, **kw):
    body = {"developer": "patrick", "scope": [], **kw}
    resp = await client.post("/api/sessions", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_a_restart_is_recorded_and_readable(client):
    """The whole point: another session can SEE that a unit was bounced."""
    sid = await _session(client)
    resp = await client.post("/api/restarts", json={
        "unit": "anime-studio",
        "session_id": sid,
        "reason": "20 commits behind disk; every render-path fix was inert",
        "old_pid": 1969400,
        "new_pid": 2415302,
        "before": {"commits_behind": 20},
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["unit"] == "anime-studio"
    assert body["old_pid"] == 1969400
    assert body["before"] == {"commits_behind": 20}
    assert body["developer"] == "patrick", "denormalized from the session for display"

    listed = (await client.get("/api/restarts")).json()
    assert [r["unit"] for r in listed] == ["anime-studio"]


@pytest.mark.asyncio
@pytest.mark.parametrize("written", [
    "comfyui.service", "ComfyUI", "  comfyui  ", "COMFYUI.SERVICE", "comfyui",
])
async def test_the_unit_name_is_normalized_at_the_writer(client, written):
    """Alias drift is prevented at the PRODUCER, not taught to every reader.

    `shots.composition_method` accumulated ~19 legacy alias strings because each
    writer invented its own spelling and every reader had to learn them. A unit
    that lands as 'comfyui.service' here and 'comfyui' there makes the ticket's
    core question -- "when was comfyui last bounced" -- unanswerable by a query.
    """
    await client.post("/api/restarts", json={"unit": written, "developer": "patrick"})
    listed = (await client.get("/api/restarts", params={"unit": "comfyui"})).json()
    assert len(listed) == 1, f"{written!r} must be queryable as 'comfyui'"
    assert listed[0]["unit"] == "comfyui"


@pytest.mark.asyncio
async def test_an_unknown_unit_is_still_recorded(client):
    """Fail OPEN on vocabulary: an unrecorded restart is worse than an oddly-named one.

    Rejecting unknown units would mean a new or ad-hoc service simply goes
    unrecorded, which is the exact blindness this ticket exists to remove.
    """
    resp = await client.post("/api/restarts", json={"unit": "some-new-daemon"})
    assert resp.status_code == 201
    assert resp.json()["unit"] == "some-new-daemon"


@pytest.mark.asyncio
async def test_a_restart_without_a_session_is_accepted(client):
    """The operator restarts things by hand, and that is the most important case.

    An out-of-band bounce is precisely what leaves other sessions confused, so
    session_id must be optional or the record would only ever capture the
    restarts that were already the best-behaved.
    """
    resp = await client.post("/api/restarts", json={
        "unit": "comfyui", "developer": "patrick", "reason": "operator recycled it by hand",
    })
    assert resp.status_code == 201
    assert resp.json()["session_id"] is None


@pytest.mark.asyncio
async def test_a_named_session_must_actually_exist(client):
    """Matches create_lock: a dangling session_id would make the record unattributable."""
    resp = await client.post("/api/restarts", json={
        "unit": "comfyui", "session_id": "no-such-session"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_is_newest_first_and_filterable(client):
    for unit in ("comfyui", "anime-studio", "comfyui"):
        await client.post("/api/restarts", json={"unit": unit})

    everything = (await client.get("/api/restarts")).json()
    assert len(everything) == 3
    ts = [r["created_at"] for r in everything]
    assert ts == sorted(ts, reverse=True), "most recent bounce must be the first thing seen"

    only_comfy = (await client.get("/api/restarts", params={"unit": "comfyui.service"})).json()
    assert len(only_comfy) == 2, "the filter must normalize the query side too"

    capped = (await client.get("/api/restarts", params={"limit": 1})).json()
    assert len(capped) == 1


@pytest.mark.asyncio
async def test_did_it_help_can_be_answered_after_the_fact(client):
    """The ticket's stated question needs a measurement taken LATER than the restart.

    Recycling ComfyUI-NVIDIA moved RAM 22.7 -> 47.7 GB and RSS 17.9 -> 1.27 GB, but
    those numbers only exist minutes afterwards. A single write-once row could never
    hold them, so `after` is patchable while the restart itself stays immutable.
    """
    rid = (await client.post("/api/restarts", json={
        "unit": "comfyui",
        "outcome": "in_progress",
        "before": {"ram_avail_gb": 22.7, "rss_gb": 17.9},
    })).json()["id"]

    resp = await client.patch(f"/api/restarts/{rid}", json={
        "outcome": "completed", "after": {"ram_avail_gb": 47.7, "rss_gb": 1.27},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["after"]["ram_avail_gb"] == 47.7
    assert body["before"]["ram_avail_gb"] == 22.7, "the original measurement is not clobbered"


@pytest.mark.asyncio
async def test_outcome_vocabulary_is_closed(client):
    """A closed value set is cheapest to enforce where the write happens."""
    resp = await client.post("/api/restarts", json={"unit": "comfyui", "outcome": "sorta"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_failed_restart_is_recordable(client):
    """A restart that did NOT come back is the single most urgent thing to surface."""
    resp = await client.post("/api/restarts", json={
        "unit": "comfyui-rocm", "outcome": "failed", "reason": "unit masked, start refused",
    })
    assert resp.status_code == 201
    assert resp.json()["outcome"] == "failed"


@pytest.mark.asyncio
async def test_patching_an_unknown_restart_is_404(client):
    resp = await client.patch("/api/restarts/nope", json={"outcome": "completed"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_service_claim_is_not_expressible_as_a_scope_lock(client):
    """Pins the measurement behind the new-table decision, so nobody 'simplifies'
    this later by folding service claims into scope_locks.

    'comfyui' is accepted by the glob validator yet matches no path -- an inert
    lock. The validator cannot catch it, which is why services need their own
    surface rather than a borrowed one.
    """
    sid = await _session(client)
    created = await client.post("/api/locks", json={
        "session_id": sid, "pattern": "comfyui", "reason": "would-be service claim"})
    assert created.status_code == 201, "the validator does NOT reject it -- that is the trap"

    check = await client.post("/api/locks/check", json={
        "paths": ["/opt/ComfyUI/main.py", "comfyui", "/etc/systemd/system/comfyui.service"]})
    assert not any(r["locked"] for r in check.json() if r["path"] != "comfyui"), (
        "a unit name protects no real path, so the lock silently guards nothing")


@pytest.mark.asyncio
async def test_age_is_computed_server_side(client):
    """SQLite drops the UTC offset, so the client cannot safely do this arithmetic.

    created_at serializes as '2026-08-24T23:42:27.712692' -- naive -- even though the
    column is DateTime(timezone=True). A caller that parsed that and subtracted it
    from an aware datetime.now(timezone.utc) would raise TypeError, and "bounced 3
    minutes ago" is the single most useful thing this record says. So the server,
    which knows the values were written as UTC, does the subtraction.
    """
    body = (await client.post("/api/restarts", json={"unit": "comfyui"})).json()
    assert "age_seconds" in body
    assert 0 <= body["age_seconds"] < 60

    # Pin the trap itself, so nobody "simplifies" age_seconds away by parsing
    # created_at on the client: this is the TypeError that would follow.
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(body["created_at"])
    if parsed.tzinfo is None:
        with pytest.raises(TypeError):
            datetime.now(timezone.utc) - parsed


@pytest.mark.asyncio
async def test_a_malformed_state_blob_does_not_break_the_history(client):
    """One bad row must not make every other restart unreadable."""
    from ai_team_sync.routers.restarts import _loads
    assert _loads("not json") == {}
    assert _loads("[1,2]") == {}, "a non-object payload is not a state dict"
    assert _loads("") == {}
    assert _loads('{"ram_avail_gb": 47.7}') == {"ram_avail_gb": 47.7}
