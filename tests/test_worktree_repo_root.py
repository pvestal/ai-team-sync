"""Repo-root resolution inside a linked git worktree.

A linked worktree's `.git` is a FILE ('gitdir: <main>/.git/worktrees/<name>'),
not a directory. The hooks walked up looking for a `.git` DIRECTORY, so from a
worktree they sailed past its root to the nearest ancestor that had one — on
this box, the operator's ~/Documents. Every edit made from a worktree then
reported a path prefixed with the worktree's directory name and anchored to an
unrelated repo, which silently disabled both the lock guard and the claim
guard for exactly the isolated-workspace flow the worktree skills encourage.
"""

from ai_team_sync.git_utils import resolve_repo_roots
from ai_team_sync.hooks.post_tool_use_presence import _display_path
from ai_team_sync.hooks.pre_tool_use_lockcheck import _rel, _roots, find_conflicts


def _make_worktree(tmp_path):
    """outer repo > main repo + linked worktree — mirrors ~/Documents on this box."""
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)

    main = outer / "code" / "proj"
    (main / ".git" / "worktrees" / "wt-feature").mkdir(parents=True)
    (main / "src").mkdir(parents=True)
    (main / "src" / "x.py").write_text("x")

    wt = outer / "wt-feature"
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt-feature\n")
    (wt / "src" / "x.py").write_text("x")
    return main, wt


def test_plain_checkout_is_its_own_worktree_and_repo_root(tmp_path):
    main, _ = _make_worktree(tmp_path)

    assert resolve_repo_roots(main / "src" / "x.py") == (str(main), str(main))


def test_linked_worktree_resolves_to_itself_and_its_shared_repo(tmp_path):
    main, wt = _make_worktree(tmp_path)

    # worktree root makes the path repo-relative; repo root is the SHARED root,
    # so a lock taken from the main checkout still binds here.
    assert resolve_repo_roots(wt / "src" / "x.py") == (str(wt), str(main))


def test_submodule_gitfile_anchors_to_the_submodule(tmp_path):
    outer = tmp_path / "outer"
    (outer / ".git" / "modules" / "sub").mkdir(parents=True)
    sub = outer / "sub"
    (sub / "src").mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")

    # No /worktrees/ segment: a submodule is its own project, not a checkout
    # of the parent, so it anchors to itself rather than to `outer`.
    assert resolve_repo_roots(sub / "src" / "y.py") == (str(sub), str(sub))


def test_unreadable_gitfile_falls_back_to_the_directory_that_holds_it(tmp_path):
    root = tmp_path / "weird"
    (root / "src").mkdir(parents=True)
    (root / ".git").write_text("not a gitdir pointer\n")

    assert resolve_repo_roots(root / "src" / "z.py") == (str(root), str(root))


def test_no_repo_anywhere_up_tree(tmp_path):
    loose = tmp_path / "loose" / "a.txt"
    loose.parent.mkdir(parents=True)

    assert resolve_repo_roots(loose) == (None, None)


def test_presence_path_from_a_worktree_is_not_prefixed_by_the_worktree_dir(tmp_path):
    _, wt = _make_worktree(tmp_path)

    assert _display_path(str(wt / "src" / "x.py"), cwd=None) == "src/x.py"


def test_lockcheck_rel_and_anchor_from_a_worktree(tmp_path):
    main, wt = _make_worktree(tmp_path)

    assert _rel(str(wt / "src" / "x.py")) == "src/x.py"
    assert _roots(str(wt / "src" / "x.py"))[1] == str(main)


def test_a_lock_taken_in_the_main_checkout_blocks_the_same_file_in_a_worktree(tmp_path):
    main, wt = _make_worktree(tmp_path)
    sessions = [{
        "id": "other", "agent": "codex", "developer": "pvestal",
        "description": "editing x", "scope": ["src/**"],
        "repo_root": str(main), "status": "active",
    }]

    rel = _rel(str(wt / "src" / "x.py"))
    _, froot = _roots(str(wt / "src" / "x.py"))
    conflicts = find_conflicts(rel, sessions, "mine", file_repo_root=froot)

    assert conflicts, "worktree edit must see the main checkout's lock"
