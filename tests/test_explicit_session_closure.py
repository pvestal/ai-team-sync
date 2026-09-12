"""Who may terminate which coordination record, decided explicitly.

`complete_session` took only a summary, resolved its target through the ambient
pointer, and never named the row it closed. On 2026-09-11 that completed the
WRONG session: a delegated Claude's SessionStart overwrote the shared
~/.ats_session, a Codex parent resolved through it, and the completion landed on
the child's row while reporting success.

The pointer layer was hardened afterwards and held — two Codex delegations ran in
this session without either touching the parent's pointer. But that is inference.
A caller that knows which session it means should be able to say so and be
REFUSED if the system disagrees.

The rule, and the subtlety that makes it correct:

    Explicit session identity wins, but any conflicting pointer that CLAIMS TO
    REPRESENT THIS CALLER must be surfaced and validated, never silently
    substituted.

A shared global pointer does not claim to represent the caller. That is why a
delegated Codex child completing itself while the global file still names its
Claude parent is NOT a conflict — treating it as one would refuse exactly the
case the isolation work exists to support.
"""

from __future__ import annotations

import pytest

from ai_team_sync.session_target import (CALLER_POINTER_SOURCES,
                                         ownership_refusal,
                                         resolve_completion_target)

PARENT = "755b5f43-d10d-444d-8846-32e5027a1b8e"   # Claude parent
CHILD = "d216ef05-289f-4e1a-8d5b-f4b4c1de0b80"    # Codex delegated child
OTHER = "0a49c287-693b-428b-a84c-0876da36f98a"    # an unrelated session

CLAUDE = "claude-code:6a76804f"
CODEX_CHILD = "codex:delegate"


# ── the operator's frozen regressions ────────────────────────────────────────

def test_claude_parent_explicitly_completes_itself():
    r = resolve_completion_target(explicit_id=PARENT, pointer_id=PARENT,
                                  pointer_source="per_session")
    assert r.ok and r.session_id == PARENT
    assert r.source == "explicit"
    assert r.conflict is None


@pytest.mark.parametrize("shared_source", ["global"])
def test_codex_child_completes_itself_while_the_global_pointer_names_claude(shared_source):
    """THE isolation case. The child's own id arrives via $ATS_SESSION_ID; the
    shared file still names the parent. That must not be read as a conflict."""
    r = resolve_completion_target(explicit_id=CHILD, pointer_id=PARENT,
                                  pointer_source=shared_source,
                                  pointer_names_live_session=True)
    assert r.ok, "a SHARED pointer must never block a child completing itself"
    assert r.session_id == CHILD
    assert r.conflict and PARENT in r.conflict, "the discrepancy is still reported"


def test_a_conflicting_caller_pointer_on_a_live_session_refuses():
    """Two live claims about who this caller is. Guessing is the original bug."""
    for source in sorted(CALLER_POINTER_SOURCES):
        r = resolve_completion_target(explicit_id=CHILD, pointer_id=PARENT,
                                      pointer_source=source,
                                      pointer_names_live_session=True)
        assert not r.ok, source
        assert PARENT in r.refusal and CHILD in r.refusal
        assert r.session_id is None, "a refusal must name no target"


def test_a_conflicting_caller_pointer_on_a_DEAD_session_yields_to_the_explicit_id():
    """A stale pointer left by a finished session is noise, not a competing claim."""
    r = resolve_completion_target(explicit_id=CHILD, pointer_id=PARENT,
                                  pointer_source="per_session",
                                  pointer_names_live_session=False)
    assert r.ok and r.session_id == CHILD
    assert r.conflict and "no longer active" in r.conflict


def test_unknown_liveness_is_treated_as_live_and_refuses():
    """Fail closed: a conflict we cannot disprove is not waved through."""
    r = resolve_completion_target(explicit_id=CHILD, pointer_id=PARENT,
                                  pointer_source="env",
                                  pointer_names_live_session=None)
    assert not r.ok


def test_parent_cannot_complete_child_without_authority():
    row = {"id": CHILD, "agent": CODEX_CHILD, "status": "active"}
    refusal = ownership_refusal(CHILD, row, CLAUDE)
    assert refusal and CODEX_CHILD in refusal and CLAUDE in refusal


def test_child_cannot_complete_parent():
    row = {"id": PARENT, "agent": CLAUDE, "status": "active"}
    refusal = ownership_refusal(PARENT, row, "codex", delegation_child_id=CHILD)
    assert refusal, "the delegation binding authorises the CHILD row, not the parent's"
    assert PARENT in refusal


def test_the_delegation_binding_lets_a_child_complete_its_own_row():
    """Exact authority: ATS created that row for this delegation. A label
    comparison cannot do this — child_env writes the label."""
    row = {"id": CHILD, "agent": CODEX_CHILD, "status": "active"}
    assert ownership_refusal(CHILD, row, "codex", delegation_child_id=CHILD) is None


def test_a_session_that_cannot_be_read_is_refused_not_assumed():
    """Nonexistent id: fails without mutation, because a row nobody fetched
    cannot be shown to be ours."""
    refusal = ownership_refusal("no-such-session", None, CLAUDE)
    assert refusal and "not found" in refusal


def test_owning_your_own_row_is_allowed():
    row = {"id": PARENT, "agent": CLAUDE, "status": "active"}
    assert ownership_refusal(PARENT, row, CLAUDE) is None


# ── the legacy path must keep working, and keep refusing ─────────────────────

def test_legacy_pointer_path_still_resolves_for_existing_callers():
    r = resolve_completion_target(explicit_id=None, pointer_id=PARENT,
                                  pointer_source="per_session")
    assert r.ok and r.session_id == PARENT
    assert r.source == "per_session", "the resolved id must be reported explicitly"


def test_legacy_path_still_refuses_the_shared_pointer():
    """The 2026-09-11 defect, still refused on the path that had it."""
    r = resolve_completion_target(explicit_id=None, pointer_id=OTHER,
                                  pointer_source="global")
    assert not r.ok
    assert "SHARED" in r.refusal


def test_no_id_and_no_pointer_refuses_rather_than_guessing():
    r = resolve_completion_target(explicit_id=None, pointer_id=None,
                                  pointer_source="none")
    assert not r.ok and "refusing to guess" in r.refusal.lower()


def test_in_process_identity_is_a_caller_pointer():
    """The strongest source: this process started that session, so a disagreement
    with an explicit id is a genuine conflict."""
    assert "in_process" in CALLER_POINTER_SOURCES
    r = resolve_completion_target(explicit_id=CHILD, pointer_id=PARENT,
                                  pointer_source="in_process",
                                  pointer_names_live_session=True)
    assert not r.ok


def test_explicit_id_matching_the_pointer_is_never_a_conflict():
    for source in ("in_process", "env", "per_session", "global"):
        r = resolve_completion_target(explicit_id=PARENT, pointer_id=PARENT,
                                      pointer_source=source)
        assert r.ok and r.conflict is None, source


# ── row-level guarantees, against a real server ──────────────────────────────

# Fields derived from the wall clock at read time, not persisted state. Comparing
# them would make every "nothing changed" assertion flap.
_VOLATILE = {"idle_seconds"}


def _persisted(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _VOLATILE}


async def _mk(client, agent, desc="s"):
    r = await client.post("/api/sessions", json={
        "developer": "tester", "agent": agent, "scope": [],
        "description": desc, "auto_lock": False})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_completing_one_session_changes_exactly_that_row(client):
    """Every other session must be byte-identical afterwards."""
    a = await _mk(client, "claude-code:aaaa", "the one being completed")
    b = await _mk(client, "claude-code:bbbb", "an unrelated live session")
    c = await _mk(client, "codex:delegate", "another unrelated session")

    before = {sid: _persisted((await client.get(f"/api/sessions/{sid}")).json())
              for sid in (b, c)}

    r = await client.patch(f"/api/sessions/{a}",
                           json={"status": "completed", "summary": "done"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == a, "the response must name the row actually mutated"
    assert r.json()["status"] == "completed"

    for sid in (b, c):
        after = _persisted((await client.get(f"/api/sessions/{sid}")).json())
        assert after["status"] == "active"
        assert after == before[sid], f"{sid} was mutated by an unrelated completion"


@pytest.mark.asyncio
async def test_a_parent_completion_leaves_the_child_row_untouched(client):
    """The 2026-09-11 shape: the parent closes and the child must not move."""
    parent = await _mk(client, "claude-code:aaaa", "parent")
    child = await _mk(client, "codex:delegate", "delegated child")
    await client.patch(f"/api/sessions/{child}",
                       json={"status": "completed",
                             "summary": "delegated READ_ONLY: inspect one function"})
    child_before = _persisted((await client.get(f"/api/sessions/{child}")).json())

    await client.patch(f"/api/sessions/{parent}",
                       json={"status": "completed", "summary": "parent done"})

    child_after = _persisted((await client.get(f"/api/sessions/{child}")).json())
    assert child_after == child_before
    assert child_after["summary"].startswith("delegated READ_ONLY")


@pytest.mark.asyncio
async def test_an_open_delegation_blocks_its_parents_completion(client):
    """Ownership cannot be dropped while a child is still out — the guarantee
    that keeps a delegation from being a silent handoff."""
    parent = await _mk(client, "claude-code:aaaa", "parent")
    d = await client.post("/api/delegations", json={
        "parent_session_id": parent, "delegated_worker": "codex",
        "mode": "READ_ONLY", "objective": "look", "acceptance": "a citation",
        "resolved_binary": "/usr/bin/codex", "launch_spec_version": "1"})
    assert d.status_code == 201, d.text

    r = await client.patch(f"/api/sessions/{parent}",
                           json={"status": "completed", "summary": "premature"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "open_delegations"

    still = (await client.get(f"/api/sessions/{parent}")).json()
    assert still["status"] == "active", "a refused completion must not mutate"


@pytest.mark.asyncio
async def test_the_server_would_restamp_a_second_completion(client):
    """Documents WHY the MCP layer refuses to re-complete a terminal session.

    The endpoint is not idempotent: a second PATCH re-stamps completed_at and
    re-emits the completion event, inventing a second closure of one session.
    The tool guards this; this test pins the behaviour it is guarding.
    """
    s = await _mk(client, "claude-code:aaaa")
    first = (await client.patch(f"/api/sessions/{s}",
                                json={"status": "completed",
                                      "summary": "real"})).json()
    second = (await client.patch(f"/api/sessions/{s}",
                                 json={"status": "completed",
                                       "summary": "accidental re-run"})).json()
    assert second["summary"] == "accidental re-run"
    assert second["completed_at"] >= first["completed_at"]
