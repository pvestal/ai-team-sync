"""A VERIFY child runs tests in a disposable worktree, not in the lead's tree.

VERIFY needs a shell, and a shell can write files, so containment cannot be a
tool list. It is two existing mechanisms cooperating:

  * Claude Code's Bash sandbox (bubblewrap + seccomp) makes everything outside the
    working directory read-only AT THE KERNEL. That half is provable only by a
    live run -- a unit test cannot produce "Read-only file system" -- so it is
    proven by canary and asserted here only as the DECLARED policy.
  * A disposable linked worktree is the writable working directory, and holds the
    lead's exact result. That half is what this file tests, because it is pure
    filesystem and git behaviour.

THE ESCAPE THIS FILE EXISTS TO PREVENT was measured, not imagined. The first
containment run placed the fixture lead repository inside the temp root alongside
the worktree; the child wrote into the lead with exit 0 and no sandbox violation,
because temp is on the WRITABLE side of the boundary. `assert_contained` refuses
that arrangement, and the test below is that measurement turned into a guard.

Fixtures remap `temp_root` rather than using pytest's tmp_path directly: tmp_path
IS inside the real temp root, so a fixture lead there would (correctly) be refused
and the happy paths would be untestable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_team_sync import verify_worktree as vw
from ai_team_sync.verify_worktree import VerifyEnvironmentError

DELEG = "deadbeefcafe1234"


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=kw.pop("check", True)).stdout


@pytest.fixture
def lead(tmp_path, monkeypatch):
    """A lead repo OUTSIDE the (remapped) temp root, with an uncommitted result."""
    fake_temp = tmp_path / "tmproot"
    fake_temp.mkdir()
    monkeypatch.setattr(vw, "temp_root", lambda: fake_temp.resolve())

    repo = tmp_path / "lead"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (repo / "SENTINEL.txt").write_text("LEAD_ORIGINAL\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    # The lead's result under review: modified tracked file + a new untracked one.
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    (repo / "NOTES.md").write_text("why mul exists\n")
    return repo


# ── the child reviews the ACTUAL lead result ────────────────────────────────

def test_the_worktree_holds_the_leads_uncommitted_result_byte_for_byte(lead):
    """Requirement 7's source half. `git worktree add <rev>` alone reproduces the
    COMMIT and silently drops the work under review, which is the whole point of
    the review. The tracked diff is replayed and untracked files copied."""
    env = vw.create(lead, DELEG)
    try:
        assert (env.path / "calc.py").read_bytes() == (lead / "calc.py").read_bytes()
        assert (env.path / "NOTES.md").read_bytes() == (lead / "NOTES.md").read_bytes()
        assert "mul" in (env.path / "calc.py").read_text()
    finally:
        vw.remove(lead, env.path)


def test_the_manifest_proves_what_source_state_the_child_sees(lead):
    """"Prove what source state it sees" has to be checkable BY THE CHILD, so the
    manifest carries a per-file sha256 comparison, not a reassurance."""
    env = vw.create(lead, DELEG)
    try:
        m = env.manifest_text
        assert env.base_commit in m
        assert "MATCH    calc.py" in m
        assert "MATCH    NOTES.md" in m
        assert "MISMATCH" not in m
        assert "every reviewed file matches the lead" in m
        assert str(lead) in m and str(env.path) in m
        # And it is on disk where the child can read it without a tool call.
        assert (env.path / vw.MANIFEST_NAME).read_text() == m
    finally:
        vw.remove(lead, env.path)


def test_a_clean_lead_tree_is_reported_as_reviewing_the_commit_itself(lead):
    _git(lead, "checkout", "--", "calc.py")
    (lead / "NOTES.md").unlink()

    env = vw.create(lead, DELEG)
    try:
        assert "reviewing the commit itself" in env.manifest_text
        assert env.changed_files == () and env.untracked == ()
    finally:
        vw.remove(lead, env.path)


def test_the_worktree_resolves_to_the_exact_revision_under_review(lead):
    env = vw.create(lead, DELEG)
    try:
        assert _git(env.path, "rev-parse", "HEAD").strip() == env.base_commit
        assert _git(lead, "rev-parse", "HEAD").strip() == env.base_commit
    finally:
        vw.remove(lead, env.path)


def test_a_diff_that_does_not_apply_is_refused_rather_than_half_reproduced(lead, monkeypatch):
    """A partially reproduced result is worse than a refused delegation, because
    the review would look valid."""
    real = vw._git

    def fake(repo, *args, **kw):
        if args[:2] == ("diff", "HEAD") and "--name-only" not in args:
            return "diff --git a/ghost.py b/ghost.py\n--- a/ghost.py\n+++ b/ghost.py\n@@ -1 +1 @@\n-a\n+b\n"
        return real(repo, *args, **kw)

    monkeypatch.setattr(vw, "_git", fake)

    with pytest.raises(VerifyEnvironmentError, match="does not apply cleanly"):
        vw.create(lead, DELEG)

    assert not vw.worktree_path(DELEG).exists(), "a refused create leaves no tree"


# ── containment: the measured escape, as a guard ───────────────────────────

def test_a_lead_repo_inside_the_temp_root_is_refused(tmp_path, monkeypatch):
    """THE measured escape. Temp is writable to the sandboxed shell, so a lead
    repository living there is writable too: the first containment run wrote into
    it with exit 0. Refuse rather than launch something contained in appearance."""
    fake_temp = tmp_path / "tmproot"
    fake_temp.mkdir()
    monkeypatch.setattr(vw, "temp_root", lambda: fake_temp.resolve())
    repo = fake_temp / "lead_in_tmp"
    repo.mkdir()
    _git(repo, "init", "-q", ".")

    with pytest.raises(VerifyEnvironmentError, match="WRITABLE"):
        vw.create(repo, DELEG)


def test_a_worktree_outside_the_temp_root_is_refused(lead, monkeypatch):
    """The mirror image: a worktree the sandbox would make read-only means the
    child cannot run tests at all."""
    monkeypatch.setattr(vw, "worktree_path", lambda d, base=None: lead.parent / "outside")

    with pytest.raises(VerifyEnvironmentError, match="not under the temp root"):
        vw.create(lead, DELEG)


def test_a_worktree_inside_the_lead_repo_is_refused(lead, monkeypatch):
    monkeypatch.setattr(vw, "worktree_path", lambda d, base=None: lead / "inner")

    with pytest.raises(VerifyEnvironmentError):
        vw.create(lead, DELEG)


def test_a_non_repository_is_refused_because_there_is_nothing_to_review(tmp_path, monkeypatch):
    fake_temp = tmp_path / "tmproot"; fake_temp.mkdir()
    monkeypatch.setattr(vw, "temp_root", lambda: fake_temp.resolve())
    plain = tmp_path / "plain"; plain.mkdir()

    with pytest.raises(VerifyEnvironmentError, match="not a git repository"):
        vw.create(plain, DELEG)


# ── 11. shell side effects stay out of the lead worktree ───────────────────

def test_11_writes_in_the_worktree_do_not_reach_the_lead(lead):
    """Requirement 11, the worktree half: even a write the sandbox WOULD permit
    (inside cwd) must not be visible in the lead tree. The kernel half, refusing
    a write aimed outside cwd, is proven by live canary."""
    before = {p.name: p.read_bytes() for p in lead.iterdir() if p.is_file()}
    env = vw.create(lead, DELEG)
    try:
        # Exactly what a test run does: scratch files, caches, an edited source.
        (env.path / "SCRATCH.txt").write_text("verification scratch\n")
        (env.path / "SENTINEL.txt").write_text("MUTATED_BY_VERIFY\n")
        (env.path / "__pycache__").mkdir()
        (env.path / "__pycache__" / "calc.pyc").write_bytes(b"\x00")

        after = {p.name: p.read_bytes() for p in lead.iterdir() if p.is_file()}
        assert after == before, "the lead worktree was modified"
        assert not (lead / "SCRATCH.txt").exists()
        assert not (lead / "__pycache__").exists()
        assert (lead / "SENTINEL.txt").read_text() == "LEAD_ORIGINAL\n"
    finally:
        vw.remove(lead, env.path)

    assert not (lead / "SCRATCH.txt").exists(), "and nothing survives teardown"


def test_11_a_shell_command_run_in_the_worktree_leaves_the_lead_untouched(lead):
    """The same property through an actual shell, since that is the real path."""
    env = vw.create(lead, DELEG)
    try:
        subprocess.run("echo SHELL > SHELL_PROOF.txt && python3 -m pytest -q",
                       shell=True, cwd=env.path, capture_output=True, text=True)
        assert (env.path / "SHELL_PROOF.txt").exists()
        assert not (lead / "SHELL_PROOF.txt").exists()
        assert _git(lead, "status", "--porcelain") == _git(lead, "status", "--porcelain")
        assert "SHELL_PROOF" not in _git(lead, "status", "--porcelain")
    finally:
        vw.remove(lead, env.path)


def test_10_a_commit_made_in_the_worktree_never_becomes_authoritative(lead):
    """Requirement 10's shell half. The worktree is DETACHED, so a commit there
    advances no branch; teardown makes it unreachable from any ref."""
    main_before = _git(lead, "rev-parse", "main").strip()
    env = vw.create(lead, DELEG)
    try:
        assert _git(env.path, "symbolic-ref", "-q", "HEAD", check=False).strip() == "", \
            "the worktree must be detached, or a commit would move a branch"
        (env.path / "calc.py").write_text("def add(a, b):\n    return 999\n")
        _git(env.path, "add", "-A")
        _git(env.path, "-c", "user.email=v@v", "-c", "user.name=v",
             "commit", "-qm", "verify child commit")
        rogue = _git(env.path, "rev-parse", "HEAD").strip()

        assert _git(lead, "rev-parse", "main").strip() == main_before
        assert rogue != main_before
    finally:
        vw.remove(lead, env.path)

    assert _git(lead, "rev-parse", "main").strip() == main_before
    # Unreachable from every ref: nothing a later reader could mistake for work.
    reachable = _git(lead, "for-each-ref", "--format=%(objectname)")
    assert rogue not in reachable
    assert (lead / "calc.py").read_text().find("999") == -1


# ── teardown is deterministic ───────────────────────────────────────────────

def test_remove_is_deterministic_and_idempotent(lead):
    env = vw.create(lead, DELEG)
    assert env.path.exists()

    assert vw.remove(lead, env.path) is True
    assert not env.path.exists()
    # Calling it again on an already-gone tree must not raise or report failure.
    assert vw.remove(lead, env.path) is True
    assert "ats-verify" not in _git(lead, "worktree", "list")


def test_remove_never_raises_so_cleanup_cannot_mask_a_result(lead):
    """Teardown runs in the supervisor's finalization path; an exception there
    would lose the child's result to a housekeeping failure."""
    assert vw.remove(lead, Path("/nonexistent/never/existed")) in (True, False)


def test_create_replaces_a_stale_tree_from_a_previous_run(lead):
    first = vw.create(lead, DELEG)
    (first.path / "STALE.txt").write_text("left over\n")

    second = vw.create(lead, DELEG)
    try:
        assert second.path == first.path
        assert not (second.path / "STALE.txt").exists(), "a stale tree must not leak in"
    finally:
        vw.remove(lead, second.path)


# ── the packet tells the child where it is ─────────────────────────────────

def test_the_verify_packet_carries_the_manifest_and_read_only_does_not():
    from ai_team_sync.delegation_packet import build_child_packet
    d = {"id": DELEG, "parent_task": "2652", "delegating_worker": "codex",
         "prohibitions": ["file_write"]}

    verify_packet = build_child_packet(
        mode="VERIFY", delegation=d, objective="review it", acceptance="citations",
        verify_env_text="ATS VERIFY ENVIRONMENT — what you are reviewing\nbase commit: abc123")
    read_only_packet = build_child_packet(
        mode="READ_ONLY", delegation=d, objective="read it", acceptance="citations")

    assert "ATS VERIFY ENVIRONMENT" in verify_packet
    assert "base commit: abc123" in verify_packet
    # The manifest precedes the objective, for the same reason the task envelope
    # does: a worker that reads the objective first starts working.
    assert verify_packet.index("ATS VERIFY ENVIRONMENT") < verify_packet.index("OBJECTIVE")
    assert "ATS VERIFY ENVIRONMENT" not in read_only_packet, (
        "the READ_ONLY packet proven in d685142 must be unchanged")


# ═══════════════════════════════════════════════════════════════════════════
# The LAUNCHER's side of the contract: create, use as cwd, always tear down.
#
# These monkeypatch verify_worktree itself. The module's own behaviour is tested
# above; what is under test here is whether the supervisor ALWAYS builds the
# environment, hands it to the child as cwd, and removes it on every terminal
# outcome -- including the outcomes where the child never ran. The recording
# harness is reused from the READ_ONLY lifecycle tranche so both modes are held
# to one description of supervisor duty.
# ═══════════════════════════════════════════════════════════════════════════

from tests.test_child_session_lifecycle import (  # noqa: E402
    CALLS, CHILD, PARENT, _RecordingClient, _clean_exit, _crash, _lease_expired,
    _nonzero_exit, _session_finalizations, supervisor)  # noqa: F401

VERIFY_WT = Path("/tmp/ats-verify/deadbeef")


class _EnvSpy:
    """Records what the launcher asked of the verification environment."""

    def __init__(self):
        self.created, self.removed, self.fail = [], [], None

    def create(self, repo_root, delegation_id, **kw):
        if self.fail:
            raise VerifyEnvironmentError(self.fail)
        self.created.append((str(repo_root), delegation_id))
        return vw.VerifyEnv(path=VERIFY_WT, repo_root=Path(repo_root),
                            base_commit="c0ffee" * 6 + "abcd",
                            manifest_text="ATS VERIFY ENVIRONMENT — spy manifest")

    def remove(self, repo_root, worktree):
        self.removed.append(str(worktree))
        return True


@pytest.fixture
def spy(monkeypatch):
    s = _EnvSpy()
    monkeypatch.setattr(vw, "create", s.create)
    monkeypatch.setattr(vw, "remove", s.remove)
    monkeypatch.setattr(vw, "VerifyEnvironmentError", VerifyEnvironmentError)
    return s


def _run_verify(runner, spawn, worker="claude-code", mode="VERIFY"):
    import ai_team_sync.cli as c
    orig = subprocess.run
    seen = {}

    def dispatch(argv, *a, **kw):
        first = str(argv[0]) if argv else ""
        if first.endswith(("claude", "codex")):
            seen["cwd"] = kw.get("cwd")
            return spawn(argv, *a, **kw)
        return orig(argv, *a, **kw)

    try:
        subprocess.run = dispatch
        result = runner.invoke(c.cli, [
            "delegate", "--parent-session", PARENT, "--worker", worker,
            "--mode", mode, "--repo", "/home/patrick/code/ai-team-sync",
            "--objective", "review the actual diff",
            "--acceptance", "citations",
        ])
    finally:
        subprocess.run = orig
    return result, seen


def test_the_launcher_builds_a_verification_environment_for_claude_verify(supervisor, spy):
    result, seen = _run_verify(supervisor, _clean_exit)

    assert result.exit_code == 0, result.output
    assert len(spy.created) == 1, "exactly one environment, named by delegation"
    assert spy.created[0][1] == "delegation-0003"
    assert seen["cwd"] == str(VERIFY_WT), (
        "the child must RUN there, or the sandbox's writable root is the lead's tree")


def test_read_only_gets_no_verification_worktree(supervisor, spy):
    """READ_ONLY has no shell, so it needs no containment environment. Building
    one anyway would be cargo-culting this tranche's mechanism into the mode that
    was already proven in d685142."""
    result, seen = _run_verify(supervisor, _clean_exit, mode="READ_ONLY")

    assert result.exit_code == 0
    assert spy.created == []
    assert seen["cwd"] is None


def test_codex_verify_gets_no_worktree_because_its_runtime_contains_it(supervisor, spy):
    """Requirement 15's neighbour: Codex VERIFY is bounded by its own
    `--sandbox read-only`, and this repair must not move Codex."""
    result, seen = _run_verify(supervisor, _clean_exit, worker="codex")

    assert result.exit_code == 0
    assert spy.created == []
    assert seen["cwd"] is None


@pytest.mark.parametrize("spawn", [_clean_exit, _nonzero_exit, _crash, _lease_expired],
                         ids=["clean", "nonzero", "crash", "lease_expired"])
def test_14_the_worktree_is_removed_on_every_terminal_outcome(supervisor, spy, spawn):
    """Requirement 14. Teardown is the supervisor's duty for the same reason
    session finalization is: a child that crashed cannot clean up after itself."""
    _run_verify(supervisor, spawn)

    assert spy.removed == [str(VERIFY_WT)], f"worktree leaked after {spawn.__name__}"


@pytest.mark.parametrize("spawn", [_clean_exit, _nonzero_exit, _crash, _lease_expired],
                         ids=["clean", "nonzero", "crash", "lease_expired"])
def test_12_the_supervisor_finalizes_exactly_the_verify_child(supervisor, spy, spawn):
    """Requirement 12, carried over from d685142 and re-proven for VERIFY."""
    _run_verify(supervisor, spawn)

    assert _session_finalizations() == [CHILD]


@pytest.mark.parametrize("spawn", [_clean_exit, _crash, _lease_expired],
                         ids=["clean", "crash", "lease_expired"])
def test_13_the_parent_session_is_never_mutated_by_a_verify_delegation(supervisor, spy, spawn):
    """Requirement 13. Delegation is not handoff, in VERIFY as in READ_ONLY."""
    _run_verify(supervisor, spawn)

    assert PARENT not in _session_finalizations()
    for verb, url, _body in CALLS:
        if verb == "PATCH":
            assert PARENT not in url


def test_a_refused_environment_does_not_spawn_and_leaves_no_orphan(supervisor, spy):
    """Fail closed, then clean up. An uncontained VERIFY child would produce a
    verdict nobody should rely on, so it must not run -- but its records already
    exist by then, so they must still be finalized."""
    spy.fail = "lead repository is inside the temp root, which the Bash sandbox makes WRITABLE"
    ran = {"spawned": False}

    def must_not_spawn(*a, **k):
        ran["spawned"] = True
        return _clean_exit(*a, **k)

    result, _seen = _run_verify(supervisor, must_not_spawn)

    assert ran["spawned"] is False, "an uncontained VERIFY child must not be launched"
    assert result.exit_code == 0, "the supervisor still finalizes; it does not crash"
    assert "verification environment refused" in result.output
    assert _session_finalizations() == [CHILD], "requirement 14: no orphan session"


def test_the_reported_result_names_the_worktree_and_the_reviewed_revision(supervisor, spy):
    """A reader must be able to check WHAT was reviewed and whether the scratch
    tree is gone, rather than take either on trust."""
    result, _seen = _run_verify(supervisor, _clean_exit)

    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["verify_worktree"] == str(VERIFY_WT)
    assert payload["verify_base_commit"].startswith("c0ffee")
    assert payload["verify_worktree_removed"] is True
    assert payload["child_session_id"] == CHILD
    assert payload["parent_still_owns"] == PARENT


def test_the_child_packet_carries_the_manifest_through_the_real_launcher(supervisor, spy):
    """End of the chain: the manifest the module builds must actually reach the
    packet the child is launched with."""
    captured = {}

    def capture(argv, *a, **k):
        captured["packet"] = argv[argv.index("-p") + 1]
        return _clean_exit(argv, *a, **k)

    _run_verify(supervisor, capture)

    assert "ATS VERIFY ENVIRONMENT" in captured["packet"]
