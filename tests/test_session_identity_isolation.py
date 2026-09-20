"""Cross-agent session identity isolation.

PROVEN FAILURE (2026-09-11, live): a Codex parent session delegated a READ_ONLY
subtask; the delegated Claude process ran its own SessionStart, which wrote the
shared ~/.ats_session pointer; the Codex parent had no process-local pointer of
its own (it has no CLAUDE_CODE_SESSION_ID), so its next complete_session
resolved through the shared file and completed the CHILD's auto-registered row.
The MCP reported "All locks released" while the parent stayed active holding its
lock.

The class: one running agent must never redirect another agent's ATS mutations
through shared state. A mutation whose identity cannot be proven process-local
must fail closed.

  A = the parent (Codex-shaped: no cid, identity held in its own process)
  B = an unrelated concurrent session
  C = the delegated child, handed an explicit session id
"""

from __future__ import annotations

import os

import pytest

from ai_team_sync import session_pointer as sp
from ai_team_sync.mcp.server import mutation_refusal


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ATS_SESSION_ID", raising=False)
    monkeypatch.delenv("ATS_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return tmp_path


# --- resolution provenance -------------------------------------------------

def test_an_explicit_env_binding_is_process_local(state, monkeypatch):
    monkeypatch.setenv("ATS_SESSION_ID", "sess-C")

    assert sp.resolve_pointer_source() == ("sess-C", "env")


def test_a_per_session_pointer_is_process_local(state, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cid-A")
    sp.save_pointer("sess-A", "cid-A")

    assert sp.resolve_pointer_source("cid-A") == ("sess-A", "per_session")


def test_the_shared_file_is_reported_as_shared(state):
    sp.global_pointer_path().write_text("sess-B")

    assert sp.resolve_pointer_source() == ("sess-B", "global")


def test_b_writing_the_shared_pointer_does_not_redirect_a(state, monkeypatch):
    """The exact live failure, at the resolution layer."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cid-A")
    sp.save_pointer("sess-A", "cid-A")

    # B starts and writes the shared file, as every session start does.
    sp.global_pointer_path().write_text("sess-B")

    assert sp.resolve_pointer_source("cid-A") == ("sess-A", "per_session")


def test_c_adopts_exactly_the_id_it_was_given(state, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cid-C")
    sp.save_pointer("sess-A", "cid-C")          # a stale pointer in C's own dir
    sp.global_pointer_path().write_text("sess-B")
    monkeypatch.setenv("ATS_SESSION_ID", "sess-C")

    sid, source = sp.resolve_pointer_source("cid-C")

    assert (sid, source) == ("sess-C", "env"), "the delegated id outranks every file"


# --- the mutation guard ----------------------------------------------------

MINE = "codex"
ROW_MINE = {"id": "sess-A", "agent": MINE, "status": "active"}
ROW_THEIRS = {"id": "sess-B", "agent": "claude-code:other", "status": "active"}


def test_in_process_identity_needs_no_further_proof():
    assert mutation_refusal("sess-A", "in_process", None, MINE) is None


def test_a_mutation_resolved_only_through_the_shared_file_is_refused():
    reason = mutation_refusal("sess-B", "global", ROW_MINE, MINE)

    assert reason is not None
    assert "shared" in reason.lower()


def test_a_mutation_with_no_identity_at_all_fails_closed():
    reason = mutation_refusal(None, "none", None, MINE)

    assert reason is not None
    assert "no" in reason.lower()


def test_an_explicit_binding_to_another_workers_row_is_refused():
    """Binding by id is not enough if the id names somebody else's session."""
    reason = mutation_refusal("sess-B", "env", ROW_THEIRS, MINE)

    assert reason is not None
    assert "claude-code:other" in reason


def test_an_explicit_binding_to_my_own_row_is_allowed():
    assert mutation_refusal("sess-A", "env", ROW_MINE, MINE) is None


def test_a_vanished_row_fails_closed():
    reason = mutation_refusal("sess-gone", "per_session", None, MINE)

    assert reason is not None


# --- end to end over the API ----------------------------------------------

async def _mk(client, agent, scope, desc):
    r = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": agent, "scope": scope,
        "description": desc, "repo_root": "/opt/anime-studio", "auto_lock": True})
    assert r.status_code == 201, r.text
    if not hasattr(client, "session_tokens"):
        client.session_tokens = {}
    client.session_tokens[r.json()["id"]] = r.headers["X-ATS-Approval-Token"]
    return r.json()


@pytest.mark.asyncio
async def test_completing_one_session_never_releases_another_sessions_locks(client):
    a = await _mk(client, "codex", ["src/a/**"], "A owns a task")
    b = await _mk(client, "claude-code:bbbb", ["src/b/**"], "B unrelated")
    c = await _mk(client, "claude-code:delegate", [], "C delegated child")

    done_c = await client.patch(f"/api/sessions/{c['id']}",
                                json={"status": "completed", "summary": "child returned"},
                                headers={"X-ATS-Approval-Token": client.session_tokens[c["id"]]})
    assert done_c.status_code == 200

    a_after = (await client.get(f"/api/sessions/{a['id']}")).json()
    b_after = (await client.get(f"/api/sessions/{b['id']}")).json()
    assert a_after["status"] == "active" and a_after["lock_count"] == a["lock_count"]
    assert b_after["status"] == "active" and b_after["lock_count"] == b["lock_count"]

    done_a = await client.patch(f"/api/sessions/{a['id']}",
                                json={"status": "completed", "summary": "A done"},
                                headers={"X-ATS-Approval-Token": client.session_tokens[a["id"]]})
    assert done_a.status_code == 200

    b_final = (await client.get(f"/api/sessions/{b['id']}")).json()
    c_final = (await client.get(f"/api/sessions/{c['id']}")).json()
    assert b_final["status"] == "active", "completing A must not touch B"
    assert c_final["status"] == "completed"


@pytest.mark.asyncio
async def test_a_delegated_child_produces_exactly_one_row(client, state, monkeypatch):
    """Autostart handed an explicit session must adopt it, not register a second."""
    from ai_team_sync.hooks import session_autostart

    parent = await _mk(client, "codex", ["src/**"], "parent owns the task")
    d = await client.post("/api/delegations", json={
        "parent_session_id": parent["id"], "delegated_worker": "claude-code",
        "mode": "READ_ONLY", "objective": "investigate", "acceptance": "cites file:line",
        "repo_root": "/opt/anime-studio"})
    child = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:delegate", "scope": [],
        "description": "delegated child", "repo_root": "/opt/anime-studio",
        "delegation_id": d.json()["id"]})
    child_id = child.json()["id"]

    before = len((await client.get("/api/sessions")).json())
    monkeypatch.setenv("ATS_SESSION_ID", child_id)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cid-child")

    adopted = await session_autostart.ensure_session("http://test", client)

    assert adopted == child_id, "the child adopts the row ATS already made for it"
    after = (await client.get("/api/sessions")).json()
    assert len(after) == before, "no second placeholder row for one delegated job"


# --- the delegated child's environment -------------------------------------

def test_the_child_env_isolates_state_and_names_its_own_session():
    from ai_team_sync.delegation import child_env

    base = {"ATS_SESSION": "leftover-from-parent", "ATS_STATE_DIR": "/parent/state",
            "PATH": "/usr/bin"}

    env = child_env(base, delegation_id="dddddddd-1111", child_session_id="sess-C",
                    worker="claude-code")

    assert env["ATS_SESSION_ID"] == "sess-C", "the child adopts its delegated row"
    assert env["ATS_STATE_DIR"] != "/parent/state", "its pointers must not be the parent's"
    assert "dddddddd" in env["ATS_STATE_DIR"]
    assert "ATS_SESSION" not in env, "ATS_SESSION is read as a Claude cid, not a session id"
    assert env["ATS_AGENT"] == "claude-code:delegate"
    assert env["PATH"] == "/usr/bin", "the rest of the environment is inherited"


def test_the_child_cannot_reach_the_parents_pointer_files(tmp_path, monkeypatch):
    """With its own state dir, the child's writes are invisible to the parent."""
    from ai_team_sync.delegation import child_env

    parent_state = tmp_path / "parent"
    parent_state.mkdir()
    monkeypatch.setenv("ATS_STATE_DIR", str(parent_state))
    sp.global_pointer_path().write_text("sess-A")

    env = child_env(dict(os.environ), delegation_id="eeeeeeee-2222",
                    child_session_id="sess-C", worker="claude-code")

    monkeypatch.setenv("ATS_STATE_DIR", env["ATS_STATE_DIR"])
    child_view = sp.global_pointer_path()
    assert not child_view.exists() or child_view.read_text().strip() != "sess-A"

    # and writing as the child leaves the parent's pointer untouched
    child_view.write_text("sess-C")
    monkeypatch.setenv("ATS_STATE_DIR", str(parent_state))
    assert sp.global_pointer_path().read_text().strip() == "sess-A"
