#!/usr/bin/env python3
"""pre-merge-commit hook: the commit guard for a merge git concludes by itself
(#3581 part B). git >= 2.24 runs it for a merge that produced no conflicts; a
conflicted merge ends in `git commit`, which runs pre-commit instead.

Paths checked: the index against HEAD -- which, when this hook runs, already
holds the merge result -- plus HEAD...MERGE_HEAD when MERGE_HEAD exists. (git
2.43 has not written MERGE_HEAD yet at this point; measured. The staged set is
then the whole effect of the merge.)

NOT covered: a fast-forward merge creates no merge commit and runs no
pre-merge-commit at all; neither do cherry-pick, revert, rebase or `git am`.
See docs/commit-guard.md for the layer that sees those (the Bash guard) and its
own limits. `git merge --no-verify` skips this hook.
"""

from __future__ import annotations

import sys

from ai_team_sync.hooks import commit_guard as cg


def main() -> None:
    try:
        paths = set(cg.staged_paths())
    except cg.GuardUnavailable as exc:
        print(f"{cg.TAG} cannot list merge paths: {exc}", file=sys.stderr)
        sys.exit(1)
    paths.update(cg.merge_incoming_paths())
    sys.exit(cg.run_git_guard("pre-merge-commit", sorted(paths)))


if __name__ == "__main__":
    main()
