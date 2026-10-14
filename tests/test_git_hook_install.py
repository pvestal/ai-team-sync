"""ATS git-hook installer (#3581 part B): common git dir, chaining, idempotence,
post hooks never clobbered. Temporary repositories only."""

from __future__ import annotations

import os
import subprocess

import pytest
from click.testing import CliRunner

from ai_team_sync import git_hook_install as ghi

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=check,
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    (r / "a.txt").write_text("a\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


def _hook(repo, name, body):
    path = repo / ".git" / "hooks" / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_install_from_a_worktree_writes_the_common_hooks_dir(repo):
    wt = repo.parent / "wt"
    git(repo, "worktree", "add", "-q", "-b", "wtb", str(wt))
    report = ghi.install(wt, python="/opt/fake/python")
    common = repo / ".git" / "hooks"
    assert report.hooks_dir == str(common)
    assert report.created == ["pre-commit", "pre-merge-commit"]
    assert "/opt/fake/python -m ai_team_sync.hooks.pre_commit" in (common / "pre-commit-ats").read_text()
    assert "ai_team_sync.hooks.pre_merge_commit" in (common / "pre-merge-commit-ats").read_text()


def test_install_is_idempotent(repo):
    first = ghi.install(repo, python="/opt/fake/python")
    before = {p.name: p.read_text() for p in (repo / ".git" / "hooks").iterdir()}
    second = ghi.install(repo, python="/opt/fake/python")
    after = {p.name: p.read_text() for p in (repo / ".git" / "hooks").iterdir()}
    assert first.created == ["pre-commit", "pre-merge-commit"]
    assert second.unchanged == ["pre-commit", "pre-merge-commit"] and not second.created
    assert before == after


def test_existing_pre_commit_is_chained_and_runs_first(repo, tmp_path):
    marker = tmp_path / "ran"
    original = f"#!/bin/sh\necho existing >> {marker}\nexit 0\n"
    _hook(repo, "pre-commit", original)
    report = ghi.install(repo, ("pre-commit",), python="/bin/true")
    assert report.chained == ["pre-commit"]
    hooks = repo / ".git" / "hooks"
    assert (hooks / "pre-commit.ats-chained").read_text() == original
    # The runner is /bin/true here, so the commit succeeds iff the chain works.
    (repo / "a.txt").write_text("b\n")
    git(repo, "commit", "-qam", "x")
    assert marker.read_text() == "existing\n"


def test_chained_hook_failure_fails_the_commit_and_ats_runner_failure_too(repo):
    _hook(repo, "pre-commit", "#!/bin/sh\necho no >&2\nexit 3\n")
    ghi.install(repo, ("pre-commit",), python="/bin/true")
    (repo / "a.txt").write_text("c\n")
    assert git(repo, "commit", "-qam", "x", check=False).returncode != 0
    (repo / ".git" / "hooks" / "pre-commit.ats-chained").write_text("#!/bin/sh\nexit 0\n")
    ghi.install(repo, ("pre-commit",), python="/bin/false")  # refresh runner
    assert git(repo, "commit", "-qam", "x", check=False).returncode != 0


def test_existing_post_commit_is_appended_to_never_clobbered(repo):
    original = "#!/usr/bin/env bash\necho existing-audit\n"
    hook = _hook(repo, "post-commit", original)
    report = ghi.install(repo, ("post-commit",), python="/opt/fake/python")
    assert report.appended == ["post-commit"]
    body = hook.read_text()
    assert body.startswith(original)
    assert body.count("post-commit-ats") == 1
    ghi.install(repo, ("post-commit",), python="/opt/fake/python")
    assert hook.read_text() == body
    assert not (repo / ".git" / "hooks" / "post-commit.ats-chained").exists()


def test_non_shell_post_commit_is_left_untouched(repo):
    original = "#!/usr/bin/env python3\nprint('audit')\n"
    hook = _hook(repo, "post-commit", original)
    report = ghi.install(repo, ("post-commit",), python="/opt/fake/python")
    assert [name for name, _ in report.skipped] == ["post-commit"]
    assert hook.read_text() == original


def test_refuses_when_chain_slot_is_taken(repo):
    _hook(repo, "pre-commit", "#!/bin/sh\nexit 0\n")
    _hook(repo, "pre-commit.ats-chained", "#!/bin/sh\nexit 0\n")
    report = ghi.install(repo, ("pre-commit",), python="/bin/true")
    assert [name for name, _ in report.skipped] == ["pre-commit"]
    assert (repo / ".git" / "hooks" / "pre-commit").read_text() == "#!/bin/sh\nexit 0\n"


def test_uninstall_restores_chained_and_strips_appended(repo):
    pre = "#!/bin/sh\nexit 0\n"
    post = "#!/bin/bash\necho audit\n"
    _hook(repo, "pre-commit", pre)
    _hook(repo, "post-commit", post)
    ghi.install(repo, ("pre-commit", "pre-merge-commit", "post-commit"), python="/bin/true")
    ghi.uninstall(repo)
    hooks = repo / ".git" / "hooks"
    assert (hooks / "pre-commit").read_text() == pre
    assert not (hooks / "pre-merge-commit").exists()
    assert (hooks / "post-commit").read_text() == post
    assert not any(p.name.endswith("-ats") for p in hooks.iterdir())


def test_cli_hooks_install_uses_the_module(repo):
    from ai_team_sync.cli import cli

    result = CliRunner().invoke(cli, ["hooks", "install", str(repo), "--python", "/bin/true"])
    assert result.exit_code == 0, result.output
    assert "created: pre-commit, pre-merge-commit" in result.output
    again = CliRunner().invoke(cli, ["hooks", "install", str(repo)])
    assert "already installed: pre-commit, pre-merge-commit" in again.output


def test_unknown_hook_is_rejected(repo):
    with pytest.raises(ghi.InstallError):
        ghi.install(repo, ("pre-push",))
