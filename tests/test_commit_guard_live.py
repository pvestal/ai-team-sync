"""#3581 part B, end to end: real git repositories, the real ATS app served by
uvicorn on loopback with a temporary SQLite DB, and every hook run as the
subprocess git or Claude Code would run.

Two sessions: A holds a live lock on f.txt; B holds one on g.txt. The scenario
letters match the ticket's acceptance list.

The governed repository lives under ~/.cache, not /tmp: the existing edit guard
(pre_tool_use_lockcheck) deliberately ignores every path under /tmp/, so a /tmp
repository could never exercise scenario (a).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ai_team_sync import git_hook_install as ghi
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
CID_A = "aaaaaaaa-0000-0000-0000-000000000001"
CID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def _git(repo, *args, env=None, check=True):
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True,
                          text=True, check=check)


@pytest.fixture
def world(live_ats, tmp_path):  # noqa: F811
    url, client, server_env = live_ats
    base = Path(tempfile.mkdtemp(prefix="ats-guard-", dir=_cache_dir()))
    try:
        gov = base / "gov"
        free = base / "free"
        base_env = {k: v for k, v in os.environ.items() if not k.startswith(("ATS_", "CLAUDE"))}
        base_env.update(
            PYTHONPATH=str(SOURCE), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@t")
        for repo in (gov, free):
            _git(base, "init", "-q", "-b", "main", str(repo), env=base_env)
            for name in ("f.txt", "g.txt", "adv.txt"):
                (repo / name).write_text(f"{name} v1\n")
            _git(repo, "add", ".", env=base_env)
            _git(repo, "commit", "-qm", "init", env=base_env)
            ghi.install(repo, ("pre-commit", "pre-merge-commit"), python=sys.executable)
        gov_real = os.path.realpath(gov)
        config = tmp_path / "operator.toml"
        config.write_text(f'[governance]\nrepositories = ["{gov_real}"]\n')
        state = tmp_path / "state"
        state.mkdir(mode=0o700)
        audit = tmp_path / "audit.log"
        base_env.update(ATS_SERVER_URL=url, ATS_OPERATOR_CONFIG=str(config),
                        ATS_STATE_DIR=str(state), ATS_COMMIT_GUARD_AUDIT_LOG=str(audit))

        tokens = {}

        def session(agent, repo_root):
            made = client.post("/api/sessions", json={
                "developer": "pvestal", "agent": agent, "scope": [], "repo_root": repo_root})
            assert made.status_code == 201, made.text
            tokens[made.json()["id"]] = made.headers["X-ATS-Approval-Token"]
            return made.json()["id"]

        def complete(sid):
            done = client.patch(f"/api/sessions/{sid}", json={"status": "completed"},
                                headers={"X-ATS-Approval-Token": tokens[sid]})
            assert done.status_code == 200, done.text

        def lock(sid, pattern, mode):
            made = client.post("/api/locks", json={"session_id": sid, "pattern": pattern,
                                                  "mode": mode, "reason": "test claim"})
            assert made.status_code == 201, made.text

        a = session("claude-code:aaaaaaaa", gov_real)
        b = session("claude-code:bbbbbbbb", gov_real)
        lock(a, "f.txt", "exclusive")
        lock(a, "adv.txt", "advisory")
        lock(b, "g.txt", "exclusive")
        a_free = session("claude-code:aaaaaaaa", os.path.realpath(free))
        lock(a_free, "f.txt", "exclusive")

        def env(sid=None, **extra):
            e = dict(base_env)
            if sid:
                e["ATS_SESSION_ID"] = sid
            e.update(extra)
            return e

        def claude_env(cid, pointer_sid, **extra):
            """B's environment as a Claude Bash tool gives it: the Claude session
            id, and that session's per-session ATS pointer on disk."""
            (state / f".ats_session_{cid[:8]}").write_text(pointer_sid)
            return env(None, CLAUDE_CODE_SESSION_ID=cid, **extra)

        yield {"gov": gov, "free": free, "a": a, "b": b, "env": env, "audit": audit,
               "url": url, "session": session, "complete": complete, "lock": lock,
               "claude_env": claude_env, "gov_real": gov_real}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _cache_dir() -> str:
    d = Path.home() / ".cache"
    d.mkdir(exist_ok=True)
    return str(d)


def _dead_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _bash_guard(command, cwd, env, cid=CID_B):
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "cwd": str(cwd), "session_id": cid}
    return subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_bashguard"],
                          input=json.dumps(payload), env=env, capture_output=True, text=True,
                          timeout=60)


def _commit(repo, env, path="f.txt", content="changed\n", msg="change"):
    (Path(repo) / path).write_text(content)
    _git(repo, "add", path, env=env)
    return _git(repo, "commit", "-m", msg, env=env, check=False)


# (a) ------------------------------------------------------------------------------

def test_a_write_tool_by_b_is_blocked_by_existing_edit_guard(world):
    payload = {"tool_name": "Write", "session_id": CID_B,
               "tool_input": {"file_path": str(world["gov"] / "f.txt")}}
    r = subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_lockcheck"],
                       input=json.dumps(payload), env=world["env"](world["b"]),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2, r.stderr
    assert "ATS" in r.stderr


# (b) + (f) --------------------------------------------------------------------------

def test_b_sed_in_place_by_b_blocked_and_by_a_allowed(world):
    cmd = "sed -i 's/v1/v2/' f.txt"
    r = _bash_guard(cmd, world["gov"], world["env"](world["b"]))
    assert r.returncode == 2, r.stderr
    assert "claude-code:aaaaaaaa" in r.stderr and world["a"] in r.stderr
    assert "EXCLUSIVE" in r.stderr

    ok = _bash_guard(cmd, world["gov"], world["env"](world["a"]), cid=CID_A)
    assert ok.returncode == 0, ok.stderr


def test_b_redirect_and_advisory_lock_also_block_b_in_governed_repo(world):
    r = _bash_guard("echo x >> adv.txt", world["gov"], world["env"](world["b"]))
    assert r.returncode == 2, r.stderr
    assert "ADVISORY" in r.stderr


def test_bash_guard_claim_check_for_direct_target_without_own_lock(world):
    """B writes a file nobody holds: no foreign lock, but B holds no covering lock
    either -- the edit guard's claim_check applies to single-file targets."""
    r = _bash_guard("echo x > new.txt", world["gov"], world["env"](world["b"]))
    assert r.returncode == 2, r.stderr
    assert "CLAIM GUARD" in r.stderr
    own = _bash_guard("echo x > g.txt", world["gov"], world["env"](world["b"]))
    assert own.returncode == 0, own.stderr


# (c) -------------------------------------------------------------------------------

def test_c_git_apply_touching_f_by_b_blocked(world):
    gov, env_b = world["gov"], world["env"](world["b"])
    (gov / "f.txt").write_text("patched\n")
    patch = _git(gov, "diff", env=env_b).stdout
    _git(gov, "checkout", "--", "f.txt", env=env_b)
    patch_file = gov.parent / "p.patch"
    patch_file.write_text(patch)

    r = _bash_guard(f"git apply {patch_file}", gov, env_b)
    assert r.returncode == 2, r.stderr
    assert "f.txt" in r.stderr and "git apply" in r.stderr
    r2 = _bash_guard(f"git apply < {patch_file}", gov, env_b)
    assert r2.returncode == 2, r2.stderr
    ok = _bash_guard(f"git apply {patch_file}", gov, world["env"](world["a"]), cid=CID_A)
    assert ok.returncode == 0, ok.stderr


# (d) -------------------------------------------------------------------------------

def _diverge(gov, env):
    _git(gov, "checkout", "-qb", "side", env=env)
    (gov / "f.txt").write_text("from side\n")
    _git(gov, "commit", "-qam", "side changes f", env=env)
    _git(gov, "checkout", "-q", "main", env=env)
    (gov / "g.txt").write_text("main moved\n")
    _git(gov, "commit", "-qam", "main changes g", env=env)


def test_d_merge_of_branch_changing_f_blocked_by_bash_guard_and_pre_merge_commit(world):
    gov = world["gov"]
    env_b = world["env"](world["b"])
    # Set up history as a human would (no session): g.txt is B's, so allow via override.
    setup = world["env"](None, ATS_COMMIT_GUARD_OVERRIDE="test setup")
    _diverge(gov, setup)
    head = _git(gov, "rev-parse", "HEAD", env=env_b).stdout.strip()

    r = _bash_guard("git merge --no-edit side", gov, env_b)
    assert r.returncode == 2, r.stderr
    assert "f.txt" in r.stderr and "git merge" in r.stderr

    m = _git(gov, "merge", "--no-edit", "side", env=env_b, check=False)
    assert m.returncode != 0, m.stdout + m.stderr
    assert "REFUSED (pre-merge-commit)" in m.stderr
    assert _git(gov, "rev-parse", "HEAD", env=env_b).stdout.strip() == head
    _git(gov, "merge", "--abort", env=env_b, check=False)

    ok = _git(gov, "merge", "--no-edit", "side", env=world["env"](world["a"]), check=False)
    assert ok.returncode == 0, ok.stderr
    assert (gov / "f.txt").read_text() == "from side\n"


def test_d_conflicted_merge_concluded_by_commit_is_caught_by_pre_commit(world):
    gov = world["gov"]
    setup = world["env"](None, ATS_COMMIT_GUARD_OVERRIDE="test setup")
    _git(gov, "checkout", "-qb", "side", env=setup)
    (gov / "f.txt").write_text("side\n")
    _git(gov, "commit", "-qam", "s", env=setup)
    _git(gov, "checkout", "-q", "main", env=setup)
    (gov / "f.txt").write_text("main\n")
    _git(gov, "commit", "-qam", "m", env=setup)

    env_b = world["env"](world["b"])
    m = _git(gov, "merge", "--no-edit", "side", env=env_b, check=False)
    assert m.returncode != 0  # conflict
    (gov / "f.txt").write_text("resolved\n")
    _git(gov, "add", "f.txt", env=env_b)
    c = _git(gov, "commit", "--no-edit", env=env_b, check=False)
    assert c.returncode != 0
    assert "REFUSED (pre-commit)" in c.stderr and "f.txt" in c.stderr


# (e) + (f) -------------------------------------------------------------------------

def test_e_commit_by_b_refused_and_f_same_commit_by_a_allowed(world):
    gov = world["gov"]
    r = _commit(gov, world["env"](world["b"]))
    assert r.returncode != 0
    assert "REFUSED (pre-commit)" in r.stderr
    assert world["a"] in r.stderr and "claude-code:aaaaaaaa" in r.stderr

    ok = _git(gov, "commit", "-m", "by a", env=world["env"](world["a"]), check=False)
    assert ok.returncode == 0, ok.stderr


def test_identified_session_refused_on_advisory_but_human_only_on_exclusive(world):
    gov = world["gov"]
    b = _commit(gov, world["env"](world["b"]), path="adv.txt")
    assert b.returncode != 0 and "advisory lock 'adv.txt'" in b.stderr

    human = _git(gov, "commit", "-m", "human", env=world["env"](None), check=False)
    assert human.returncode == 0, human.stderr
    assert "WARNING (pre-commit)" in human.stderr

    excl = _commit(gov, world["env"](None), path="f.txt")
    assert excl.returncode != 0
    assert "unidentified committer" in excl.stderr and world["a"] in excl.stderr


# (g) -------------------------------------------------------------------------------

def test_g_ats_down_governed_refused_ungoverned_allowed(world):
    dead = _dead_url()
    gov = _commit(world["gov"], world["env"](world["b"], ATS_SERVER_URL=dead), path="g.txt")
    assert gov.returncode != 0
    assert "fails closed" in gov.stderr and "--no-verify" in gov.stderr

    free = _commit(world["free"], world["env"](world["b"], ATS_SERVER_URL=dead), path="f.txt")
    assert free.returncode == 0, free.stderr

    bash = _bash_guard("sed -i s/a/b/ g.txt", world["gov"],
                       world["env"](world["b"], ATS_SERVER_URL=dead))
    assert bash.returncode == 2 and "Failing closed" in bash.stderr
    bash_free = _bash_guard("sed -i s/a/b/ g.txt", world["free"],
                            world["env"](world["b"], ATS_SERVER_URL=dead))
    assert bash_free.returncode == 0, bash_free.stderr


def test_ungoverned_repo_keeps_legacy_exclusive_block(world):
    r = _commit(world["free"], world["env"](world["b"]), path="f.txt")
    assert r.returncode != 0 and "REFUSED (pre-commit)" in r.stderr


# (h) -------------------------------------------------------------------------------

def test_h_override_env_allows_and_audits(world):
    env = world["env"](world["b"], ATS_COMMIT_GUARD_OVERRIDE="operator ok #3581 test")
    r = _commit(world["gov"], env)
    assert r.returncode == 0, r.stderr
    assert "OVERRIDE (pre-commit)" in r.stderr and "operator ok #3581 test" in r.stderr
    entries = [json.loads(line) for line in world["audit"].read_text().splitlines()]
    assert entries[-1]["reason"] == "operator ok #3581 test"
    assert entries[-1]["session_id"] == world["b"]
    assert entries[-1]["paths"] == ["f.txt"]
    assert [h["session_id"] for h in entries[-1]["would_block"]] == [world["a"]]

    down = dict(env, ATS_SERVER_URL=_dead_url(),
                ATS_COMMIT_GUARD_OVERRIDE="ats down, operator ok")
    r2 = _commit(world["gov"], down, path="g.txt", content="again\n")
    assert r2.returncode == 0, r2.stderr
    last = json.loads(world["audit"].read_text().splitlines()[-1])
    assert last["reason"] == "ats down, operator ok" and "ATS unreachable" in last["note"]


# (i) -------------------------------------------------------------------------------

def test_i_worktree_of_governed_repo_is_enforced(world):
    gov = world["gov"]
    wt = gov.parent / "gov-wt"
    env_b = world["env"](world["b"])
    _git(gov, "worktree", "add", "-q", "-b", "wt-branch", str(wt), env=env_b)
    # Hooks were installed once, into the common dir; the worktree has none of its own.
    assert ghi.hooks_dir_for(wt) == ghi.hooks_dir_for(gov)

    r = _commit(wt, env_b)
    assert r.returncode != 0 and "REFUSED (pre-commit)" in r.stderr
    assert os.path.realpath(gov) in r.stderr  # anchored to the main checkout

    bash = _bash_guard("sed -i s/v1/v2/ f.txt", wt, env_b)
    assert bash.returncode == 2, bash.stderr

    ok = _git(wt, "commit", "-m", "a in wt", env=world["env"](world["a"]), check=False)
    assert ok.returncode == 0, ok.stderr


# (j) -------------------------------------------------------------------------------

def test_j_python_c_write_is_allowed_with_residual_warning(world):
    cmd = "python3 -c \"open('f.txt','w').write('x')\""
    r = _bash_guard(cmd, world["gov"], world["env"](world["b"]))
    assert r.returncode == 0, r.stderr
    assert "not checked" in r.stderr and "python3" in r.stderr


# review finding 1: session identity spoof / inheritance ----------------------------

def test_spoof_holder_id_in_env_is_refused_when_claude_pointer_names_b(world):
    """B runs `ATS_SESSION_ID=<A> git commit` from its Claude Bash tool. Before
    the fix $ATS_SESSION_ID won, A's lock became 'own', and the commit passed."""
    a, b = world["a"], world["b"]
    spoof = world["claude_env"](CID_B, b, ATS_SESSION_ID=a)
    r = _commit(world["gov"], spoof)
    assert r.returncode != 0
    assert "REFUSED (pre-commit)" in r.stderr and "contradicts" in r.stderr
    assert a in r.stderr and b in r.stderr

    g = _bash_guard("sed -i s/v1/v2/ f.txt", world["gov"], spoof, cid=CID_B)
    assert g.returncode == 2 and "contradicts" in g.stderr
    assert a in g.stderr and b in g.stderr

    # Same Claude session without the borrowed id: B is B, and A's lock refuses.
    plain = _git(world["gov"], "commit", "-m", "b", env=world["claude_env"](CID_B, b),
                 check=False)
    assert plain.returncode != 0 and "contradicts" not in plain.stderr
    assert "REFUSED (pre-commit) for session " + b in plain.stderr


def test_pointer_and_matching_env_identity_is_accepted(world):
    b = world["b"]
    r = _commit(world["gov"], world["claude_env"](CID_B, b, ATS_SESSION_ID=b), path="g.txt")
    assert r.returncode == 0, r.stderr


def test_env_only_unknown_session_refused_in_governed_repo(world):
    env = world["env"]("00000000-dead-beef-0000-000000000000")
    r = _commit(world["gov"], env, path="g.txt")
    assert r.returncode != 0
    assert "is not valid" in r.stderr and "00000000-dead-beef" in r.stderr
    g = _bash_guard("echo x >> g.txt", world["gov"], env)
    assert g.returncode == 2 and "is not valid" in g.stderr


def test_env_only_completed_session_refused_in_governed_repo(world):
    c = world["session"]("claude-code:cccccccc", world["gov_real"])
    world["complete"](c)
    r = _commit(world["gov"], world["env"](c), path="g.txt")
    assert r.returncode != 0
    assert "'completed', not active" in r.stderr


def test_env_only_invalid_identity_in_ungoverned_repo_is_judged_unidentified(world):
    env = world["env"]("00000000-dead-beef-0000-000000000000")
    ok = _commit(world["free"], env, path="g.txt")
    assert ok.returncode == 0, ok.stderr
    assert "judging as an unidentified committer" in ok.stderr
    excl = _commit(world["free"], env, path="f.txt")  # a_free's exclusive lock
    assert excl.returncode != 0 and "unidentified committer" in excl.stderr


def test_residual_same_account_env_identity_is_cooperative_not_authenticated(world):
    """DOCUMENTED RESIDUAL, not a feature: with no Claude pointer behind it, a
    process of the SAME OS account may name any ACTIVE session that account owns
    -- including the lock holder's -- and be treated as it. ATS identity is
    cooperative; docs/commit-guard.md says so. This test pins the behaviour so a
    change to it is deliberate."""
    r = _commit(world["gov"], world["env"](world["a"]))
    assert r.returncode == 0, r.stderr


# review finding 2 end to end ------------------------------------------------------

def test_cp_parents_target_is_resolved_under_the_replicated_path(world):
    world["lock"](world["a"], "mirror/sub/x.txt", "exclusive")
    r = _bash_guard("cp --parents sub/x.txt mirror/", world["gov"], world["env"](world["b"]))
    assert r.returncode == 2, r.stderr
    assert "'mirror/sub/x.txt'" in r.stderr and world["a"] in r.stderr
