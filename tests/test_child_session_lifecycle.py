"""The SUPERVISOR finalizes the child session. The child is never asked to.

Operator ruling 2026-09-12. Observed in the Codex-led canary: Claude's self-close
was blocked, and the launcher then closed the exact child session correctly. The
preferred architecture is the one that already happened by accident -- delegation
correctness must not depend on the model child remembering, or being ABLE, to
call complete_session.

Under the repaired READ_ONLY policy it is not able to: complete_session is a
coordination MUTATION and the launch spec denies it. So child self-close is
removed from the acceptance requirements rather than re-enabled, and these tests
freeze the supervisor's duty instead.

WHAT WAS ACTUALLY MISSING. The launcher already finalized on clean exit, on
non-zero exit and on lease expiry. It did NOT finalize when:

  1. `subprocess.run` raised anything other than TimeoutExpired -- a spawn that
     never started, an OSError, a signal. The exception propagated and skipped
     finalization entirely, leaving the child session ACTIVE with its delegation
     open. A child that failed to START cannot clean up after itself.
  2. The result POST failed. The two calls were sequential in one block with no
     guard, so a refused or unreachable `/return` left the session open.

Both are now unconditional. These tests are written against the observable
calls the supervisor makes, because that is what the server sees.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from ai_team_sync import cli as cli_module
from ai_team_sync import launch_spec

PARENT = "parent-session-0001"
CHILD = "child-session-0002"
DELEG = "delegation-0003"

CALLS: list[tuple[str, str, dict]] = []


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _RecordingClient:
    """Stands in for the server and records every call the supervisor makes."""

    fail_return = False          # make POST /return raise, as an outage would

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, **k):
        CALLS.append(("POST", url, json or {}))
        if url.endswith("/api/delegations"):
            return _Resp(201, {"id": DELEG, "prohibitions": ["file_write"],
                               "acceptance": "file:line citations",
                               "parent_owner_session_id": PARENT})
        if url.endswith("/return"):
            if _RecordingClient.fail_return:
                raise OSError("server unreachable during return")
            return _Resp(200, {"state": "returned"})
        if url.endswith("/api/sessions"):
            return _Resp(201, {"id": CHILD})
        if url.endswith("/api/brief"):
            return _Resp(200, {"rendered": "(brief)"})
        return _Resp(200, {})

    def patch(self, url, json=None, **k):
        CALLS.append(("PATCH", url, json or {}))
        return _Resp(200, {})


def _patch_calls():
    return [(u, b) for verb, u, b in CALLS if verb == "PATCH"]


def _session_finalizations():
    """Every call that completes a session, and which session it named."""
    out = []
    for url, body in _patch_calls():
        if "/api/sessions/" in url and body.get("status") == "completed":
            out.append(url.rsplit("/", 1)[-1])
    return out


@pytest.fixture
def supervisor(monkeypatch):
    """The delegate CLI with the server and the spawn both stubbed out."""
    CALLS.clear()
    _RecordingClient.fail_return = False
    monkeypatch.setattr(cli_module.httpx, "Client", _RecordingClient)
    # resolve_binary's `which` default is bound at def time, so patch the
    # module attribute that validate_launchable/build_launch look up at call time.
    monkeypatch.setattr(launch_spec, "resolve_binary",
                        lambda spec, which=None: f"/usr/local/bin/{spec.executable}")
    monkeypatch.setattr(cli_module, "_get_developer", lambda: "tester")
    return CliRunner()


def _run(runner, spawn, mode="READ_ONLY", worker="claude-code"):
    """Stub ONLY the delegated child spawn.

    The CLI runs its own `git` helpers through subprocess too, so replacing
    subprocess.run wholesale makes a helper raise before the spawn is reached --
    which looked exactly like the defect under test and is not it.
    """
    import ai_team_sync.cli as c
    orig = subprocess.run

    def dispatch(argv, *a, **k):
        first = str(argv[0]) if argv else ""
        if first.endswith(("claude", "codex")):
            return spawn(argv, *a, **k)
        return orig(argv, *a, **k)

    try:
        subprocess.run = dispatch
        return runner.invoke(c.cli, [
            "delegate", "--parent-session", PARENT, "--worker", worker,
            "--mode", mode, "--repo", "/opt/anime-studio",
            "--objective", "trace the closure gate",
            "--acceptance", "file:line citations, no edits",
        ])
    finally:
        subprocess.run = orig


def _clean_exit(*a, **k):
    return subprocess.CompletedProcess(a[0] if a else [], 0,
                                       stdout="found it at foo.py:12", stderr="")


def _nonzero_exit(*a, **k):
    return subprocess.CompletedProcess(a[0] if a else [], 1, stdout="", stderr="boom")


def _crash(*a, **k):
    raise OSError("Cannot allocate memory")


def _lease_expired(*a, **k):
    raise subprocess.TimeoutExpired(cmd="claude", timeout=1)


# ── 8. the supervisor closes EXACTLY the delegated child ────────────────────

def test_8_supervisor_finalizes_exactly_the_child_session(supervisor):
    result = _run(supervisor, _clean_exit)

    assert result.exit_code == 0, result.output
    assert _session_finalizations() == [CHILD], (
        "exactly one session is completed, and it is the child")


def test_8_the_result_is_submitted_as_the_child_not_the_parent(supervisor):
    _run(supervisor, _clean_exit)

    returns = [b for _v, u, b in CALLS if u.endswith("/return")]
    assert len(returns) == 1
    assert returns[0]["actor_session_id"] == CHILD
    assert "foo.py:12" in returns[0]["result_summary"]


# ── 9. the parent is untouched ──────────────────────────────────────────────

@pytest.mark.parametrize("spawn", [_clean_exit, _nonzero_exit, _crash, _lease_expired],
                         ids=["clean", "nonzero", "crash", "lease_expired"])
def test_9_the_parent_session_is_never_mutated(supervisor, spawn):
    """Delegation is not handoff: the parent keeps owning the task throughout,
    on every terminal outcome including the ones that finalize the child."""
    _run(supervisor, spawn)

    assert PARENT not in _session_finalizations()
    for url, _body in _patch_calls():
        assert PARENT not in url, f"the supervisor mutated the parent: {url}"


# ── 10. no terminal outcome leaves an orphan ────────────────────────────────

@pytest.mark.parametrize("spawn", [_clean_exit, _nonzero_exit, _crash, _lease_expired],
                         ids=["clean", "nonzero", "crash", "lease_expired"])
def test_10_every_terminal_outcome_finalizes_the_child(supervisor, spawn):
    """Requirement 10. The crash case is the one that regressed: before this
    change only TimeoutExpired was caught, so an OSError propagated and the
    child session stayed active forever."""
    result = _run(supervisor, spawn)

    assert _session_finalizations() == [CHILD], (
        f"orphan: child not finalized after {spawn.__name__} "
        f"(exit {result.exit_code}); output: {result.output[-400:]}")


def test_10_a_crashed_child_does_not_take_the_supervisor_down_with_it(supervisor):
    result = _run(supervisor, _crash)

    assert result.exit_code == 0, (
        f"the supervisor must survive its child to finalize it: {result.output[-400:]}")
    assert "child did not run" in result.output


def test_10_a_failed_result_post_still_finalizes_the_child(supervisor):
    """The second orphan path: the two calls were sequential and unguarded, so a
    refused or unreachable `/return` skipped the session close."""
    _RecordingClient.fail_return = True

    result = _run(supervisor, _clean_exit)

    assert _session_finalizations() == [CHILD]
    assert "return_error" in result.output


def test_the_reported_state_names_the_failure_rather_than_claiming_success(supervisor):
    _RecordingClient.fail_return = True

    result = _run(supervisor, _clean_exit)

    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["state"].startswith("return_error")
    assert payload["child_session_id"] == CHILD
    assert payload["parent_still_owns"] == PARENT


# ── the child is not asked to close itself ──────────────────────────────────

def test_the_child_packet_never_asks_the_child_to_close_its_session(supervisor):
    """Child self-close is out of the contract, so the packet must not imply it.

    If the packet asked for a call the READ_ONLY policy denies, every delegated
    child would end on an instruction it cannot follow.
    """
    _run(supervisor, _clean_exit)

    packets = [b for _v, u, b in CALLS if u.endswith("/api/sessions")]
    assert packets, "the child session was created"
    from ai_team_sync.delegation_packet import build_child_packet
    packet = build_child_packet(
        mode="READ_ONLY",
        delegation={"id": DELEG, "parent_task": "2652",
                    "delegating_worker": "codex", "prohibitions": ["file_write"]},
        objective="trace the closure gate", acceptance="citations")

    lowered = packet.lower()
    for phrase in ("complete_session", "close your session", "complete your session"):
        assert phrase not in lowered, f"the packet asks for {phrase!r}"


# ── the server-side half: completing a child leaves the parent alone ────────

@pytest.mark.asyncio
async def test_completing_the_child_leaves_the_parent_active(client):
    """The same contract against the real server, not a stub."""
    parent = (await client.post("/api/sessions", json={
        "developer": "tester", "agent": "codex:parent01", "scope": [],
        "description": "owns the task", "auto_lock": False})).json()

    d = (await client.post("/api/delegations", json={
        "parent_session_id": parent["id"], "parent_task": "2652",
        "delegated_worker": "claude-code", "mode": "READ_ONLY",
        "repo_root": "/opt/anime-studio", "scope": [],
        "objective": "trace the closure gate",
        "acceptance": "file:line citations", "lease_minutes": 30,
        "resolved_binary": "/usr/local/bin/claude", "launch_spec_version": "1",
    })).json()

    child_response = await client.post("/api/sessions", json={
        "developer": "tester", "agent": "claude-code:delegate", "scope": [],
        "description": "delegated READ_ONLY", "repo_root": "/opt/anime-studio",
        "delegation_id": d["id"]})
    child = child_response.json()

    r = await client.patch(f"/api/sessions/{child['id']}",
                           json={"status": "completed", "summary": "done"},
                           headers={"X-ATS-Approval-Token": child_response.headers["X-ATS-Approval-Token"]})
    assert r.status_code < 400, r.text

    assert (await client.get(f"/api/sessions/{child['id']}")).json()["status"] == "completed"
    assert (await client.get(f"/api/sessions/{parent['id']}")).json()["status"] == "active"
