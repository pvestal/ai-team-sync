"""#2757 — a reader's VERDICT must know who is asking.

The lock readers answer two different questions and only one of them is
identity-free. "Which live locks cover this path?" is a namespace question and
its answer does not depend on the caller (docs/lock-readers.md). "Is this caller
blocked?" is a verdict, and rendering it without knowing the caller is what told
a session its own exclusive claims blocked its own commit (#2756 observation),
and what listed a session's brand-new locks back to it under BLOCKERS NOW.

The invariant pinned here, identically for both readers:

  a session is never blocked by its OWN locks; foreign overlapping locks still
  block; and a caller whose identity cannot be established authoritatively gets
  the conservative answer plus an explicit statement that it is conservative.

Identity comes from the #2741 boundary — the kernel's owner of the requesting
socket — never from a caller-supplied string. A supplied session_id is a
CLAIM: it is honoured only when it belongs to the requesting account.
"""

from __future__ import annotations

import os

import pytest

from ai_team_sync import peer_identity

MINE_UID = 5150
OTHER_UID = 5151
ROOT = "/srv/echo-2757"


@pytest.fixture
def peer(monkeypatch):
    """The kernel's answer for the requesting socket, controlled per request."""
    state = {"uid": MINE_UID}
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: state["uid"])
    return state


async def _session(client, peer, uid, agent, scope, mode="advisory", repo_root=ROOT):
    peer["uid"] = uid
    resp = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": list(scope),
        "repo_root": repo_root, "description": "2757 fixture",
        "auto_lock": True, "lock_mode": mode,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _check(client, peer, uid, paths, session_id=None, repo_root=ROOT,
                 headers=None):
    peer["uid"] = uid
    body = {"staged_files": list(paths), "repo_root": repo_root}
    if session_id is not None:
        body["session_id"] = session_id
    resp = await client.post("/api/git/pre-commit-check", json=body,
                             headers=headers or {})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _brief(client, peer, uid, scope, session_id=None, repo_root=ROOT):
    peer["uid"] = uid
    body = {"objective": "2757", "repo_root": repo_root,
            "scope": list(scope), "recall": False, "limit": 8}
    if session_id is not None:
        body["session_id"] = session_id
    resp = await client.post("/api/brief", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 1. an explicit session_id, validated against the caller's account ───────

async def test_validated_session_id_excludes_its_own_exclusive_lock(client, peer):
    mine = await _session(client, peer, MINE_UID, "claude-code:mine", ["src/**"], "exclusive")

    data = await _check(client, peer, MINE_UID, ["src/a.py"], session_id=mine)

    assert data["blocking_locks"] == []
    assert data["can_proceed"] is True
    assert data["caller_identity_unresolved"] is False


# ── 2. no body session_id: the server resolves it from the caller's own
#      identifying header, the same one liveness validates ─────────────────

async def test_server_resolved_session_excludes_its_own_lock(client, peer):
    mine = await _session(client, peer, MINE_UID, "claude-code:mine", ["src/**"], "exclusive")

    data = await _check(client, peer, MINE_UID, ["src/a.py"],
                        headers={"X-ATS-Session-Id": mine})

    assert data["blocking_locks"] == []
    assert data["can_proceed"] is True
    assert data["caller_identity_unresolved"] is False


async def test_server_resolves_an_unambiguous_agent_label(client, peer):
    await _session(client, peer, MINE_UID, "claude-code:mine", ["src/**"], "exclusive")

    data = await _check(client, peer, MINE_UID, ["src/a.py"],
                        headers={"X-ATS-Agent": "claude-code:mine"})

    assert data["blocking_locks"] == []
    assert data["caller_identity_unresolved"] is False


async def test_a_caller_that_names_no_session_is_never_guessed_for(client, peer):
    """The regression that made this rule: a uid owning exactly one session is
    not proof the request came FROM it. A bare hook owns no session at all."""
    await _session(client, peer, MINE_UID, "claude-code:mine", ["src/**"], "exclusive")

    data = await _check(client, peer, MINE_UID, ["src/a.py"])

    assert data["caller_identity_unresolved"] is True
    assert data["can_proceed"] is False


# ── 3. a foreign lock still blocks, and says whose it is ───────────────────

async def test_foreign_overlapping_lock_still_blocks(client, peer):
    await _session(client, peer, OTHER_UID, "codex:theirs", ["src/**"], "exclusive")
    mine = await _session(client, peer, MINE_UID, "claude-code:mine", ["docs/**"])

    data = await _check(client, peer, MINE_UID, ["src/a.py"], session_id=mine)

    assert data["can_proceed"] is False
    assert len(data["blocking_locks"]) == 1
    assert data["caller_identity_unresolved"] is False


async def test_blocker_names_the_agent_and_session_not_only_the_human(client, peer):
    theirs = await _session(client, peer, OTHER_UID, "codex:theirs", ["src/**"], "exclusive")
    mine = await _session(client, peer, MINE_UID, "claude-code:mine", ["docs/**"])

    data = await _check(client, peer, MINE_UID, ["src/a.py"], session_id=mine)

    blocker = data["blocking_locks"][0]
    assert blocker["agent"] == "codex:theirs"
    assert blocker["session_id"] == theirs
    # The shared human name stays available as display, but is not the identity.
    assert blocker["developer"] == "pvestal"
    assert any("codex:theirs" in w for w in data["warnings"])


# ── 4. identity that cannot be established fails conservatively, and says so ─

async def test_unresolved_identity_fails_conservatively_and_reports_it(client, peer):
    await _session(client, peer, MINE_UID, "claude-code:mine", ["src/**"], "exclusive")

    data = await _check(client, peer, None, ["src/a.py"])

    assert data["caller_identity_unresolved"] is True
    assert data["can_proceed"] is False
    assert len(data["blocking_locks"]) == 1
    assert any("own locks" in w for w in data["warnings"]), data["warnings"]


async def test_one_agent_label_on_several_sessions_is_not_guessed_between(client, peer):
    """One agent routinely holds several sessions, one per repo. Ambiguity is
    unresolved — never a guess that could exclude the wrong session's lock."""
    await _session(client, peer, MINE_UID, "claude-code:one", ["src/**"], "exclusive")
    await _session(client, peer, MINE_UID, "claude-code:one", ["docs/**"],
                   repo_root="/srv/other")

    data = await _check(client, peer, MINE_UID, ["src/a.py"],
                        headers={"X-ATS-Agent": "claude-code:one"})

    assert data["caller_identity_unresolved"] is True
    assert data["can_proceed"] is False


# ── 5. a supplied session_id is a claim, not an identity ───────────────────

async def test_supplied_session_id_of_another_account_cannot_exclude(client, peer):
    theirs = await _session(client, peer, OTHER_UID, "codex:theirs", ["src/**"], "exclusive")

    data = await _check(client, peer, MINE_UID, ["src/a.py"], session_id=theirs)

    assert data["can_proceed"] is False
    assert len(data["blocking_locks"]) == 1
    assert data["caller_identity_unresolved"] is True


async def test_unknown_session_id_cannot_exclude(client, peer):
    await _session(client, peer, MINE_UID, "claude-code:mine", ["src/**"], "exclusive")

    data = await _check(client, peer, MINE_UID, ["src/a.py"],
                        session_id="00000000-0000-0000-0000-000000000000")

    assert data["can_proceed"] is False
    assert data["caller_identity_unresolved"] is True


# ── 6. the brief holds the same invariant ─────────────────────────────────

async def test_brief_excludes_own_locks_but_keeps_foreign_blockers(client, peer):
    await _session(client, peer, OTHER_UID, "codex:theirs", ["docs/**"])
    mine = await _session(client, peer, MINE_UID, "claude-code:mine", ["docs/**"])

    data = await _brief(client, peer, MINE_UID, ["docs/**"], session_id=mine)

    texts = [b["text"] for b in data["blockers"]]
    assert any("codex:theirs" in t for t in texts), texts
    assert not any("claude-code:mine" in t for t in texts), texts
    assert data["caller_identity_unresolved"] is False


async def test_brief_unresolved_identity_keeps_every_blocker_and_says_so(client, peer):
    await _session(client, peer, MINE_UID, "claude-code:mine", ["docs/**"])

    data = await _brief(client, peer, None, ["docs/**"])

    assert data["caller_identity_unresolved"] is True
    assert len(data["blockers"]) == 1
    assert "own locks" in data["rendered"], data["rendered"]
