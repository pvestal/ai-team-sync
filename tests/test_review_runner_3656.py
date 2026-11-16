"""#3656: ATS runs the review tests, inside a proven boundary, on the exact commit.

A Codex VERIFY child cannot run database-backed tests (its read-only sandbox
denies every socket family), so ATS runs a declared selection before the child
launches and hands it the raw result. These tests hold the properties that make
that result trustworthy:

  * the launcher that runs on the host comes from the trusted ref, never from
    the commit under review, and no checkout hook runs;
  * every run proves its boundary from inside it, and a run without one is
    REFUSED with no test executed (mutation tests: drop the network namespace,
    drop the tmpfs over the socket/credential directory, run unconfined);
  * the result is the exact commit's, the throwaway trees are gone afterwards,
    and the evidence reaches both the child's packet and the delegation record;
  * `ats delegate --review-tests` runs before any record exists and refuses
    (no records) when the run cannot be made trustworthy.

No test here touches the host's database: every probed socket is one the test
created itself (host discovery is replaced), and the end-to-end boundary is a
minimal bwrap launcher, skipped where unprivileged user namespaces are absent.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from ai_team_sync import review_runner as rr

PYDIR = str(Path(sys.executable).parent)


# ── fixtures: a sandbox repository whose launcher we control ────────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _write(path: Path, text: str, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    path.chmod(mode)


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


UNCONFINED = """
#!/bin/bash
# NOT a sandbox: runs the command on the host. Stands in for a launcher that
# lost its isolation, so the probe must catch it.
echo TRUSTED-LAUNCHER
[ "$1" = --exec ] && shift
export PATH="{pydir}:$PATH"
exec "$@"
"""

BWRAP = """
#!/bin/bash
# Minimal real kernel boundary for the end-to-end test.
echo TRUSTED-LAUNCHER
REPO=$(cd "$(dirname "$(readlink -f "${{BASH_SOURCE[0]}}")")/../.." && pwd)
[ "$1" = --exec ] && shift
exec bwrap --die-with-parent --unshare-user {net} --unshare-pid \\
  --ro-bind / / --dev /dev --proc /proc --tmpfs /run {hide} --tmpfs "$HOME" \\
  --setenv PATH "{pydir}:/usr/bin:/bin" --setenv PYTHONDONTWRITEBYTECODE 1 \\
  --setenv TMPDIR /run --chdir "$REPO" "$@"
"""

TESTS_OK = """
def test_good():
    assert True

def test_bad():
    assert {bad_passes}
"""


def _bwrap_usable() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind",
                           "/", "/", "true"], capture_output=True).returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_usable(),
                                 reason="bwrap with unprivileged user+net namespaces unavailable")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A sandbox repo (sandbox mode), a hosted repo, a config, and a fake DB socket.

    The fake database socket is a real listening AF_UNIX socket the test owns, in
    a directory the end-to-end launcher hides with a tmpfs, exactly as the real
    launcher hides /run (where the host Postgres socket lives).
    """
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    db_sock = hidden / ".s.PGSQL.5432"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(db_sock))
    srv.listen(8)
    secret = hidden / "pgpass"
    secret.write_text("host:5432:db:user:not-a-real-secret\n")
    # Host discovery would add the workstation's real Postgres socket; the probe
    # must never be pointed at it from a test.
    monkeypatch.setattr(rr, "host_db_sockets", lambda extra=(): list(extra))

    repo = tmp_path / "sandboxrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo / "tests" / "test_ok.py", TESTS_OK.format(bad_passes="False"))
    w = {"tmp": tmp_path, "repo": repo, "hidden": hidden, "db_sock": db_sock,
         "secret": secret, "server": srv}

    def make_config(**over):
        doc = {
            "work_dir": str(tmp_path / "work"), "log_dir": str(tmp_path / "logs"),
            "nice": 0, "db_sockets": [str(db_sock)], "secret_paths": [str(secret)],
            "sandbox": {"repo": str(repo), "trusted_ref": "main",
                        "runner_dir": "scripts/test_sandbox",
                        "entrypoint": "scripts/test_sandbox/pytest-sandbox"},
            "repos": {str(repo): {"mode": "sandbox"}},
        }
        doc.update(over)
        path = tmp_path / "review_runner.toml"
        path.write_text(_toml(doc))
        return rr.load_config(path)

    w["config"] = make_config
    yield w
    srv.close()


def _toml(doc: dict) -> str:
    lines, tables = [], []
    for key, value in doc.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {json.dumps(value)}")
    for name, table in tables:
        if name == "repos":
            for root, spec in table.items():
                lines.append(f'[repos."{root}"]')
                lines += [f"{k} = {json.dumps(v)}" for k, v in spec.items()]
        else:
            lines.append(f"[{name}]")
            lines += [f"{k} = {json.dumps(v)}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def _install_launcher(world, text, message="launcher"):
    _write(world["repo"] / "scripts" / "test_sandbox" / "pytest-sandbox",
           text.format(pydir=PYDIR, net="--unshare-net",
                       hide=f'--tmpfs "{world["hidden"]}"'), 0o755)
    return _commit(world["repo"], message)


def _worktrees(repo) -> list[str]:
    return [line for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]


# ── the boundary is proven per run, or nothing runs ─────────────────────────

def test_unconfined_launcher_is_refused_and_no_test_runs(world):
    sentinel = world["tmp"] / "a-test-ran"
    _write(world["repo"] / "tests" / "test_side_effect.py",
           f"open({str(sentinel)!r}, 'w').write('ran')\n\ndef test_x():\n    pass\n")
    _install_launcher(world, UNCONFINED)
    cfg = world["config"]()

    with pytest.raises(rr.ReviewRunError) as exc:
        rr.run_review_tests(str(world["repo"]), "main", ["tests", "-q"], config=cfg)

    msg = str(exc.value)
    assert "ISOLATION PROBE FAILED" in msg
    assert "host filesystem read-only" in msg and "WRITABLE" in msg
    assert "host database socket unreachable" in msg and "CONNECTED" in msg
    assert "credential file unreadable or masked" in msg and "READABLE" in msg
    assert not sentinel.exists(), "the probe failed, so no test may have executed"
    assert len(_worktrees(world["repo"])) == 1, "the throwaway tree was removed"


def test_no_checkout_hook_runs_on_the_host(world):
    sentinel = world["tmp"] / "hook-ran"
    hook = world["repo"] / ".git" / "hooks" / "post-checkout"
    _write(hook, f"#!/bin/sh\ntouch {sentinel}\n", 0o755)
    _install_launcher(world, UNCONFINED)

    with pytest.raises(rr.ReviewRunError):
        rr.run_review_tests(str(world["repo"]), "main", ["tests"], config=world["config"]())
    assert not sentinel.exists()


def _sentinel_driver(world, sentinel: Path):
    """A configured filter driver whose every program leaves a sentinel, as the
    system/user git-lfs driver would run git-lfs (Codex VERIFY 26b13c62)."""
    script = world["tmp"] / "filter.sh"
    _write(script, f"#!/bin/sh\ntouch {sentinel}\ncat\n", 0o755)
    for key in ("smudge", "clean"):
        _git(world["repo"], "config", f"filter.sentinel.{key}", str(script))
    _git(world["repo"], "config", "filter.sentinel.process", str(script))
    _git(world["repo"], "config", "filter.sentinel.required", "true")


def test_reviewed_gitattributes_cannot_run_a_filter_on_the_host(world):
    sentinel = world["tmp"] / "filter-ran"
    _install_launcher(world, UNCONFINED)
    _git(world["repo"], "checkout", "-q", "-b", "lfs-like")
    _write(world["repo"] / ".gitattributes", "* filter=sentinel\n")
    _write(world["repo"] / "payload.bin", "pointer-like content\n")
    _git(world["repo"], "-c", "filter.sentinel.clean=cat", "add", "-A")
    _git(world["repo"], "commit", "-q", "-m", "reviewed commit selects a filter")
    reviewed = _git(world["repo"], "rev-parse", "HEAD")
    _sentinel_driver(world, sentinel)
    os.utime(world["repo"] / "payload.bin")      # dirty stat: `git status` would clean
    with pytest.raises(rr.ReviewRunError, match="ISOLATION PROBE FAILED") as exc:
        rr.run_review_tests(str(world["repo"]), reviewed, ["tests"], config=world["config"]())
    assert exc.value.run.sha == reviewed
    assert not sentinel.exists(), "no filter program ran during checkout or status"


def test_a_driver_selected_outside_the_tree_is_neutralized_too(world):
    """Lock 2 alone: $GIT_DIR/info/attributes is not covered by GIT_ATTR_SOURCE."""
    sentinel = world["tmp"] / "filter-ran"
    _install_launcher(world, UNCONFINED)
    _sentinel_driver(world, sentinel)
    _write(world["repo"] / ".git" / "info" / "attributes", "* filter=sentinel\n")
    with pytest.raises(rr.ReviewRunError, match="ISOLATION PROBE FAILED"):
        rr.run_review_tests(str(world["repo"]), "main", ["tests"], config=world["config"]())
    assert not sentinel.exists()


def test_the_callers_git_environment_cannot_redirect_the_run(world, monkeypatch, tmp_path):
    head = _install_launcher(world, UNCONFINED)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    with pytest.raises(rr.ReviewRunError, match="ISOLATION PROBE FAILED") as exc:
        rr.run_review_tests(str(world["repo"]), "main", ["tests"], config=world["config"]())
    assert exc.value.run.sha == head and exc.value.run.repo == os.path.realpath(world["repo"])


def test_probe_interpreter_is_an_absolute_host_path(world, tmp_path):
    """Not a PATH lookup inside the sandbox, where the tested tree's venv/bin
    comes first and a committed venv/bin/python3 could print a fake PASS."""
    _install_launcher(world, UNCONFINED)
    with pytest.raises(rr.ReviewRunError) as exc:
        rr.run_review_tests(str(world["repo"]), "main", ["tests"], config=world["config"]())
    argv = exc.value.run.argv
    assert argv[argv.index("--exec") + 1] == "/usr/bin/python3"
    with pytest.raises(rr.ReviewRunError, match="probe_python"):
        world["config"](probe_python=str(tmp_path / "no-such-python"))


def test_reviewed_commit_cannot_supply_the_launcher(world):
    """The launcher is the one host-side program; the reviewed commit's copy never runs."""
    _install_launcher(world, UNCONFINED, "trusted launcher on main")
    reviewed_ran = world["tmp"] / "reviewed-launcher-ran"
    _git(world["repo"], "checkout", "-q", "-b", "feature")
    _write(world["repo"] / "scripts" / "test_sandbox" / "pytest-sandbox",
           f"#!/bin/bash\necho REVIEWED-LAUNCHER\ntouch {reviewed_ran}\n", 0o755)
    feature = _commit(world["repo"], "reviewed change rewrites the launcher")
    _git(world["repo"], "checkout", "-q", "main")

    with pytest.raises(rr.ReviewRunError) as exc:
        rr.run_review_tests(str(world["repo"]), feature, ["tests"], config=world["config"]())

    run = exc.value.run
    log = Path(run.log_path).read_text()
    assert "TRUSTED-LAUNCHER" in log and "REVIEWED-LAUNCHER" not in log
    assert not reviewed_ran.exists()
    assert run.sha == feature
    assert run.runner_differs == ["scripts/test_sandbox/pytest-sandbox"]


def test_symlinked_runner_dir_in_the_reviewed_commit_is_refused(world):
    _install_launcher(world, UNCONFINED)
    outside = world["tmp"] / "outside"
    outside.mkdir()
    _git(world["repo"], "checkout", "-q", "-b", "evil")
    shutil.rmtree(world["repo"] / "scripts" / "test_sandbox")
    os.symlink(outside, world["repo"] / "scripts" / "test_sandbox")
    evil = _commit(world["repo"], "runner dir becomes a symlink out of the tree")
    _git(world["repo"], "checkout", "-q", "main")

    with pytest.raises(rr.ReviewRunError, match="symlink"):
        rr.run_review_tests(str(world["repo"]), evil, ["tests"], config=world["config"]())
    assert list(outside.iterdir()) == [], "nothing was written through the symlink"
    assert len(_worktrees(world["repo"])) == 1


def test_a_run_that_times_out_is_killed_and_returned_as_partial(world):
    pidfile = world["tmp"] / "pid"
    _install_launcher(world, f"#!/bin/bash\necho $$ > {pidfile}\nexec sleep 120\n")
    run = rr.run_review_tests(str(world["repo"]), "main", ["tests"],
                              config=world["config"](), timeout_s=2)
    assert run.timed_out and not run.tests_ran
    assert run.status().startswith("TIMED OUT")
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_marker_printed_twice_is_refused_as_ambiguous():
    nonce = "n" * 32
    line = f"{rr.PROBE_MARKER} {nonce} " + json.dumps({"ok": True, "checks": [{"ok": True}]})
    with pytest.raises(rr.ReviewRunError, match="2 times"):
        rr._parse_log(f"{line}\n{line}\n", nonce)
    probe, summary, failed = rr._parse_log(
        f"{line}\nFAILED tests/a.py::t - boom\nERROR tests/b.py\n"
        f"=== 1 failed, 2 passed, 1 error in 0.5s ===\n", nonce)
    assert probe["ok"] and failed == ["tests/a.py::t", "tests/b.py"]
    assert summary == "1 failed, 2 passed, 1 error in 0.5s"


# ── end to end inside a real kernel boundary ────────────────────────────────

@needs_bwrap
def test_end_to_end_exact_commit_inside_a_proven_boundary(world):
    base = _install_launcher(world, BWRAP)
    _write(world["repo"] / "tests" / "test_ok.py", TESTS_OK.format(bad_passes="True"))
    good = _commit(world["repo"], "test_bad passes here")
    _write(world["repo"] / "tests" / "test_ok.py", TESTS_OK.format(bad_passes="False"))
    head = _commit(world["repo"], "test_bad fails here")
    cfg = world["config"]()

    review = rr.run_for_review(str(world["repo"]), head, ["tests/test_ok.py", "-q",
                                                         "-p", "no:cacheprovider"],
                               base_rev=good, config=cfg)
    run = review.head
    assert run.sha == head and run.tests_ran, run.tail
    checks = {c["check"]: c for c in run.probe["checks"]}
    assert checks["network namespace holds only loopback"]["detail"] == ["lo"]
    assert "ENOENT" in checks["host database socket unreachable"]["detail"]
    assert checks["host filesystem read-only"]["ok"]
    assert run.exit_code == 1 and run.failed_ids == ["tests/test_ok.py::test_bad"]
    assert run.summary.startswith("1 failed, 1 passed")
    assert review.base.exit_code == 0
    assert review.comparison()["new_failures"] == ["tests/test_ok.py::test_bad"]

    import hashlib
    assert hashlib.sha256(Path(run.log_path).read_bytes()).hexdigest() == run.log_sha256
    assert len(_worktrees(world["repo"])) == 1
    assert list(Path(cfg.work_dir).iterdir()) == []

    packet = review.packet_text()
    for needle in ("ATS REVIEW TESTS", run.run_id, head, "isolation probe   : PASS",
                   "new failures   : tests/test_ok.py::test_bad",
                   "never from the reviewed tree"):
        assert needle in packet
    ev = review.evidence()
    assert ev["head"]["sha"] == head and ev["head"]["probe"]["ok"]
    assert ev["comparison"]["new_failures"] == ["tests/test_ok.py::test_bad"]
    assert base  # the launcher commit is an ancestor of both


@needs_bwrap
@pytest.mark.parametrize("drop, expect", [
    ("net", "network namespace holds only loopback"),
    ("hide", "host database socket unreachable"),
])
def test_mutation_removing_part_of_the_boundary_refuses(world, drop, expect):
    text = BWRAP.replace("{net}" if drop == "net" else "{hide}", "")
    _write(world["repo"] / "scripts" / "test_sandbox" / "pytest-sandbox",
           text.format(pydir=PYDIR, net="--unshare-net",
                       hide=f'--tmpfs "{world["hidden"]}"'), 0o755)
    _commit(world["repo"], f"launcher without {drop}")
    with pytest.raises(rr.ReviewRunError, match="ISOLATION PROBE FAILED") as exc:
        rr.run_review_tests(str(world["repo"]), "main", ["tests/test_ok.py"],
                            config=world["config"]())
    failed = [c["check"] for c in exc.value.run.probe["checks"] if not c["ok"]]
    assert expect in failed


# ── configuration fails closed ──────────────────────────────────────────────

def test_config_is_required_and_validated(world, tmp_path):
    with pytest.raises(rr.ReviewRunError, match="not configured"):
        rr.load_config(tmp_path / "absent.toml")
    with pytest.raises(rr.ReviewRunError, match="inside sandbox.runner_dir"):
        world["config"](sandbox={"repo": str(world["repo"]), "trusted_ref": "main",
                                 "runner_dir": "scripts/test_sandbox",
                                 "entrypoint": "bin/launch"})
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(rr.ReviewRunError, match="only for the sandbox repository"):
        world["config"](repos={str(other): {"mode": "sandbox"}})
    with pytest.raises(rr.ReviewRunError, match="plain identifier"):
        world["config"](repos={str(other): {"mode": "hosted", "python": "/usr/bin/python3",
                                            "test_database": "x; drop", "dsn_env": "D"}})
    with pytest.raises(rr.ReviewRunError, match="is inside"):
        world["config"](work_dir=str(world["repo"] / "w"))


def test_hosted_interpreter_symlink_is_kept_not_resolved(world, tmp_path):
    """A venv's bin/python is a symlink; resolving it would drop the venv."""
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)
    other = tmp_path / "hostedrepo"
    other.mkdir()
    cfg = world["config"](repos={
        str(world["repo"]): {"mode": "sandbox"},
        str(other): {"mode": "hosted", "python": str(venv_python),
                     "test_database": "echo_test_x", "dsn_env": "ECHO_TEST_DSN"}})
    assert cfg.repos[os.path.realpath(other)].python == str(venv_python)


def test_planted_work_dir_symlink_is_refused(world, tmp_path):
    _install_launcher(world, UNCONFINED)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "work").symlink_to(elsewhere)
    with pytest.raises(rr.ReviewRunError, match="not a directory owned by this user"):
        rr.run_review_tests(str(world["repo"]), "main", ["tests"], config=world["config"]())
    assert list(elsewhere.iterdir()) == []


def test_unprofiled_repository_is_refused(world, tmp_path):
    _install_launcher(world, UNCONFINED)
    stray = tmp_path / "stray"
    stray.mkdir()
    _git(stray, "init", "-q")
    with pytest.raises(rr.ReviewRunError, match="no review-runner profile"):
        rr.run_review_tests(str(stray), "HEAD", ["tests"], config=world["config"]())


# ── ats delegate --review-tests ─────────────────────────────────────────────

ORDER: list[str] = []
POSTS: list[tuple[str, dict]] = []


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code, self._payload = status_code, payload or {}
        self.text, self.headers = json.dumps(self._payload), headers or {}

    def json(self):
        return self._payload


class _Server:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, **k):
        ORDER.append("POST " + url.split("/api/", 1)[-1])
        POSTS.append((url, json or {}))
        if url.endswith("/api/delegations"):
            return _Resp(201, {"id": "deleg-1", "prohibitions": [],
                               "acceptance": "a", "parent_owner_session_id": "p"})
        if url.endswith("/api/sessions"):
            return _Resp(201, {"id": "child-1"}, {"X-ATS-Approval-Token": "tok"})
        if url.endswith("/return"):
            return _Resp(200, {"state": "returned"})
        return _Resp(200, {"rendered": ""})

    def patch(self, url, json=None, **k):
        return _Resp(200, {})


@pytest.fixture
def delegate_cli(monkeypatch, tmp_path):
    from ai_team_sync import cli as cli_module
    from ai_team_sync import launch_spec
    ORDER.clear()
    POSTS.clear()
    monkeypatch.setattr(cli_module.httpx, "Client", _Server)
    monkeypatch.setattr(launch_spec, "resolve_binary",
                        lambda spec, which=None: f"/usr/local/bin/{spec.executable}")
    monkeypatch.setattr(cli_module, "_get_developer", lambda: "tester")
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    spawned: dict = {}
    orig = subprocess.run

    def dispatch(argv, *a, **k):
        if argv and str(argv[0]).endswith(("claude", "codex")):
            ORDER.append("spawn")
            spawned["stdin"] = k.get("input", "")
            return subprocess.CompletedProcess(argv, 0, stdout="no verdict", stderr="")
        return orig(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", dispatch)

    def invoke(*extra, mode="VERIFY"):
        return CliRunner().invoke(cli_module.cli, [
            "delegate", "--parent-session", "p", "--worker", "codex", "--mode", mode,
            "--repo", str(tmp_path), "--objective", "review it", "--acceptance", "verdict",
            *extra])
    return invoke, spawned


def _fake_review(monkeypatch, *, raise_with=None):
    calls = []

    class FakeTests:
        def packet_text(self):
            return "ATS REVIEW TESTS (fake block) run r1"

        def evidence(self):
            return {"head": {"run_id": "r1", "exit_code": 0}}

    def fake(repo, rev, selection, **kw):
        ORDER.append("review-tests")
        calls.append((repo, rev, selection, kw))
        if raise_with:
            raise rr.ReviewRunError(raise_with)
        return FakeTests()

    monkeypatch.setattr(rr, "run_for_review", fake)
    return calls


def test_delegate_runs_review_tests_before_any_record_and_attaches_them(delegate_cli, monkeypatch):
    invoke, spawned = delegate_cli
    calls = _fake_review(monkeypatch)
    result = invoke("--review-tests", "tests/test_x.py -k 'a and b'", "--review-sha", "abc123",
                    "--review-base", "main")
    assert result.exit_code == 0, result.output
    assert calls[0][1] == "abc123" and calls[0][2] == ["tests/test_x.py", "-k", "a and b"]
    assert calls[0][3]["base_rev"] == "main"
    assert ORDER[0] == "review-tests" and ORDER.index("POST delegations") > 0
    assert "ATS REVIEW TESTS (fake block)" in spawned["stdin"]
    assert spawned["stdin"].index("ATS REVIEW TESTS") < spawned["stdin"].index("OBJECTIVE")
    ret = [b for u, b in POSTS if u.endswith("/return")][0]
    assert ret["evidence"]["review_tests"] == {"head": {"run_id": "r1", "exit_code": 0}}
    out = json.loads(result.output[result.output.index("{\n"):])
    assert out["review_tests"]["head"]["run_id"] == "r1"


def test_delegate_refuses_without_records_when_review_tests_cannot_run(delegate_cli, monkeypatch):
    invoke, spawned = delegate_cli
    _fake_review(monkeypatch, raise_with="ISOLATION PROBE FAILED inside the sandbox")
    result = invoke("--review-tests", "tests")
    assert result.exit_code == 5
    assert "ISOLATION PROBE FAILED" in result.output
    assert not POSTS and "spawn" not in ORDER, "no delegation, no child, no launch"


def test_review_repo_names_the_tested_repository(delegate_cli, monkeypatch, tmp_path):
    invoke, spawned = delegate_cli
    calls = _fake_review(monkeypatch)
    other = tmp_path / "other-repo"
    result = invoke("--review-tests", "tests", "--review-repo", str(other))
    assert result.exit_code == 0, result.output
    assert calls[0][0] == str(other) and calls[0][1] == "HEAD"


def test_review_tests_are_only_for_verify(delegate_cli, monkeypatch):
    invoke, _ = delegate_cli
    calls = _fake_review(monkeypatch)
    result = invoke("--review-tests", "tests", mode="READ_ONLY")
    assert result.exit_code == 2 and not calls and not POSTS


def test_delegate_without_review_tests_is_unchanged(delegate_cli, monkeypatch):
    invoke, spawned = delegate_cli
    calls = _fake_review(monkeypatch)
    result = invoke()
    assert result.exit_code == 0, result.output
    assert not calls and "ATS REVIEW TESTS" not in spawned["stdin"]
    ret = [b for u, b in POSTS if u.endswith("/return")][0]
    assert "review_tests" not in ret["evidence"]
