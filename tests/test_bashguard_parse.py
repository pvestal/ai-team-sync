"""Static write-target extraction of the Bash guard (#3581 part B).

Pure parsing plus `git` read-only queries against throwaway repositories; no
ATS server. The end-to-end verdicts live in test_commit_guard_live.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ai_team_sync.hooks.pre_tool_use_bashguard import analyze

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True,
                          capture_output=True, text=True).stdout


def paths(command, cwd):
    a = analyze(command, str(cwd))
    return ({os.path.relpath(t.path, cwd) for t in a.targets if not t.path.startswith("/dev/")},
            a.unresolved)


@pytest.mark.parametrize("command,expected", [
    ("sed -i 's/a/b/' f.txt", {"f.txt"}),
    ("sed -i.bak -e 's/a/b/' f.txt g.txt", {"f.txt", "g.txt"}),
    ("sed -Ei 's/a/b/' f.txt", {"f.txt"}),
    ("sed --in-place=.orig 's/a/b/' f.txt", {"f.txt"}),
    ("sed -n 1,5p f.txt", set()),
    ("perl -pi -e 's/a/b/' f.txt", {"f.txt"}),
    ("echo x | tee -a f.txt g.txt >/dev/null", {"f.txt", "g.txt"}),
    ("echo x > f.txt 2>&1", {"f.txt"}),
    ("echo x >> f.txt; echo y &> g.txt", {"f.txt", "g.txt"}),
    ("cp a.txt f.txt", {"f.txt"}),
    ("mv f.txt g.txt", {"f.txt", "g.txt"}),
    ("install -m 644 a.txt f.txt", {"f.txt"}),
    ("rsync -a a.txt f.txt", {"f.txt"}),
    ("rsync -a a.txt host:/x", set()),
    ("truncate -s 0 f.txt", {"f.txt"}),
    ("rm -f f.txt", {"f.txt"}),
    ("sudo -u root tee f.txt", {"f.txt"}),
    ("FOO=1 timeout 5 sed -i s/a/b/ f.txt", {"f.txt"}),
    ("cd sub && sed -i s/a/b/ f.txt", {"sub/f.txt"}),
    ("if true; then sed -i s/a/b/ f.txt; fi", {"f.txt"}),
    ("cat > f.txt <<'EOF'\nrm -rf everything\necho > nope\nEOF\necho done", {"f.txt"}),
    ("x=$(sed -i s/a/b/ f.txt)", {"f.txt"}),
    ("echo 'a > b' \"c > d\" # > e", set()),
    ("ls -la && git status && grep -r x .", set()),
])
def test_literal_targets(tmp_path, command, expected):
    got, _ = paths(command, tmp_path)
    assert got == expected


@pytest.mark.parametrize("command,needle", [
    ("python3 -c \"open('f.txt','w')\"", "python3"),
    ("node -e 'require(\"fs\").writeFileSync(\"f\",\"x\")'", "node"),
    ("bash -c 'echo x > f.txt'", "bash -c"),
    ("eval \"$CMD\"", "eval"),
    ("sed -i s/a/b/ \"$FILE\"", "variable"),
    ("find . -name '*.py' -delete", "find"),
    ("ls | xargs rm", "xargs"),
    ("perl -e 'unlink q(f)'", "perl -e"),
])
def test_unresolvable_constructs_are_reported_not_guessed(tmp_path, command, needle):
    got, unresolved = paths(command, tmp_path)
    assert got == set()
    assert any(needle in u for u in unresolved), unresolved


def test_cp_into_directory_and_recursive_copy(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.txt").write_text("1")
    (tmp_path / "dst").mkdir()
    got, _ = paths("cp -r src dst", tmp_path)
    assert got == {"dst/src/one.txt"}
    got, _ = paths("cp a.txt b.txt dst", tmp_path)
    assert got == {"dst/a.txt", "dst/b.txt"}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], env=ENV, check=True)
    for name in ("f.txt", "g.txt", "h.txt"):
        (r / name).write_text("v1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    git(r, "checkout", "-qb", "side")
    (r / "f.txt").write_text("side\n")
    git(r, "commit", "-qam", "side f")
    git(r, "checkout", "-q", "main")
    (r / "g.txt").write_text("main\n")
    git(r, "commit", "-qam", "main g")
    return r


def test_git_merge_resolves_incoming_paths_only(repo):
    got, _ = paths("git merge --no-edit side", repo)
    assert got == {"f.txt"}  # g.txt changed only on main: the merge does not touch it


def test_git_merge_without_ref_and_no_upstream_is_unresolved(repo):
    got, unresolved = paths("git merge", repo)
    assert got == set() and any("@{u}" in u for u in unresolved)


def test_git_cherry_pick_and_revert(repo):
    side = git(repo, "rev-parse", "side").strip()
    assert paths(f"git cherry-pick {side}", repo)[0] == {"f.txt"}
    assert paths("git revert --no-edit HEAD", repo)[0] == {"g.txt"}
    assert paths("git cherry-pick main..side", repo)[0] == {"f.txt"}


def test_git_rebase_and_pull_and_reset(repo):
    git(repo, "checkout", "-q", "side")
    assert paths("git rebase main", repo)[0] == {"g.txt"}
    git(repo, "checkout", "-q", "main")
    assert paths("git reset --hard side", repo)[0] == {"f.txt", "g.txt"}
    assert paths("git reset --soft side", repo)[0] == set()
    got, unresolved = paths("git pull", repo)
    assert any("pull" in u for u in unresolved)


def test_git_checkout_paths_branch_switch_and_restore(repo):
    assert paths("git checkout side -- f.txt", repo)[0] == {"f.txt"}
    assert paths("git checkout -- g.txt", repo)[0] == {"g.txt"}
    assert paths("git checkout side", repo)[0] == {"f.txt", "g.txt"}
    assert paths("git switch side", repo)[0] == {"f.txt", "g.txt"}
    assert paths("git checkout -b new", repo)[0] == set()
    assert paths("git restore --source side f.txt", repo)[0] == {"f.txt"}
    assert paths("git restore --staged f.txt", repo)[0] == set()
    assert paths("git checkout side -- '*.txt'", repo)[0] == {"f.txt", "g.txt", "h.txt"}


def test_git_stash_push_and_pop(repo):
    (repo / "h.txt").write_text("dirty\n")
    assert paths("git stash", repo)[0] == {"h.txt"}
    git(repo, "stash")
    assert paths("git stash pop", repo)[0] == {"h.txt"}
    assert paths("git stash list", repo)[0] == set()


def test_git_apply_and_am_use_numstat(repo):
    (repo / "h.txt").write_text("patched\n")
    patch = repo.parent / "p.patch"
    patch.write_text(git(repo, "diff"))
    git(repo, "checkout", "--", "h.txt")
    assert paths(f"git apply {patch}", repo)[0] == {"h.txt"}
    assert paths(f"git apply --check {patch}", repo)[0] == set()
    assert paths(f"git apply < {patch}", repo)[0] == {"h.txt"}
    mbox = repo.parent / "m.patch"
    mbox.write_text(git(repo, "format-patch", "-1", "--stdout", "side"))
    assert paths(f"git am {mbox}", repo)[0] == {"f.txt"}
    got, unresolved = paths(f"cat {patch} | git apply", repo)
    assert got == set() and any("stdin" in u for u in unresolved)


def test_git_rm_mv_clean_and_git_C(repo, tmp_path):
    assert paths("git rm f.txt", repo)[0] == {"f.txt"}
    assert paths("git rm --cached f.txt", repo)[0] == set()
    assert paths("git mv f.txt z.txt", repo)[0] == {"f.txt", "z.txt"}
    (repo / "junk.tmp").write_text("x")
    assert paths("git clean -f", repo)[0] == {"junk.tmp"}
    assert paths("git clean -n", repo)[0] == set()
    got = {os.path.relpath(t.path, repo) for t in analyze(f"git -C {repo} merge side",
                                                           str(tmp_path)).targets}
    assert got == {"f.txt"}


def test_worktree_paths_are_relative_to_the_worktree(repo):
    from ai_team_sync.hooks.pre_tool_use_bashguard import group_targets

    wt = repo.parent / "wt"
    git(repo, "worktree", "add", "-q", "-b", "wtb", str(wt))
    a = analyze("sed -i s/a/b/ f.txt", str(wt))
    groups = group_targets(a.targets)
    assert list(groups) == [os.path.realpath(repo)]
    assert [rel for rel, _ in groups[os.path.realpath(repo)]] == ["f.txt"]


def test_paths_outside_any_repo_and_inside_dot_git_are_dropped(repo, tmp_path):
    from ai_team_sync.hooks.pre_tool_use_bashguard import group_targets

    a = analyze(f"echo > {tmp_path}/loose.txt; echo >> .git/info/exclude; echo > f.txt",
                str(repo))
    groups = group_targets(a.targets)
    assert {rel for rel, _ in groups[os.path.realpath(repo)]} == {"f.txt"}
    assert len(groups) == 1


def test_files_under_skips_git_dir(repo):
    from ai_team_sync.hooks.pre_tool_use_bashguard import _files_under

    assert not any("/.git/" in p for p in _files_under(str(repo)))
    assert Path(repo / "f.txt").as_posix() in _files_under(str(repo))
