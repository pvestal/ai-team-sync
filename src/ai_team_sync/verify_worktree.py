"""A disposable worktree a VERIFY child can run tests in, without reaching the lead.

WHY THIS EXISTS
---------------
VERIFY is the one mode that legitimately needs a SHELL: it runs the test suite,
reads `git diff`, runs linters. A shell can write files, so no tool allow-list can
truthfully call VERIFY filesystem-read-only. Claiming otherwise would be inventing
fake read-only shell semantics.

Containment is therefore ENVIRONMENTAL, and it is two existing mechanisms
cooperating, neither of them invented here:

  1. Claude Code's own Bash sandbox (bubblewrap + seccomp on Linux), enabled via
     `sandbox.enabled` with `failIfUnavailable` so a missing backend REFUSES
     rather than silently running the shell unconfined. Measured 2026-09-12: a
     write outside the working directory fails at the kernel with
     "Read-only file system", not at a prompt and not at the model's discretion.
  2. A linked git worktree, which gives the child a WRITABLE working directory
     holding the exact source under review. Side effects land there and die with
     it.

THE INVARIANT THE SANDBOX GIVES US, AND ITS ONE SHARP EDGE
---------------------------------------------------------
Measured writable set: the working directory and the system temp root. Everything
else, $HOME included, is read-only. So the arrangement that contains a VERIFY
child is: worktree under the temp root, lead repository NOT under it.

That edge is not hypothetical. The first containment measurement put the fixture
lead repo in the temp root too, and the child wrote into it with exit 0 — because
temp is writable, so a lead repo living there is writable as well. `assert_contained`
refuses that arrangement instead of launching a VERIFY child that only looks
contained.

WHY A PATCH RATHER THAN JUST A REVISION
---------------------------------------
A VERIFY child must review the ACTUAL lead result, and the lead result is usually
still UNCOMMITTED when review happens: the canary flow is Codex implements, then
Claude verifies, before anything is committed. `git worktree add <rev>` reproduces
the commit and silently drops exactly the work under review. So the tracked diff
is replayed and untracked files are copied, and the manifest records a per-file
sha256 comparison so "the child saw the real thing" is checkable rather than
asserted. A patch that does not apply is a hard failure: a partially reproduced
result is worse than a refused delegation, because the review would look valid.

NO VERIFY COMMIT CAN BECOME AUTHORITATIVE
-----------------------------------------
The worktree is checked out DETACHED, so a commit made in it advances no branch.
`remove()` runs `git worktree remove --force` and then prunes, so the commit is
unreachable from any ref and collectable. The lead's HEAD, branches and index are
never written by anything here.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Directory name inside the worktree is avoided on purpose: the manifest is a
# dotfile so it cannot be mistaken for part of the source under review, and so a
# `git status` in the worktree does not show a file the supervisor created.
MANIFEST_NAME = ".ats-verify-manifest.txt"


class VerifyEnvironmentError(RuntimeError):
    """The verification environment cannot be built or cannot be trusted.

    Raised instead of launching. A VERIFY child that is not actually contained,
    or that is not actually looking at the lead's result, produces a review whose
    conclusion cannot be relied on — which is worse than no review.
    """


@dataclass
class VerifyEnv:
    path: Path
    repo_root: Path
    base_commit: str
    manifest_text: str
    diff_sha256: str = ""
    untracked: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = field(default=())

    @property
    def created(self) -> bool:
        return self.path.exists()


def _git(repo: str | Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise VerifyEnvironmentError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def assert_contained(worktree: Path, repo_root: Path) -> None:
    """Refuse an arrangement the sandbox would not actually contain.

    The sandbox's writable set is the working directory plus the temp root. A lead
    repository inside the temp root is therefore writable from the child's shell,
    and a VERIFY run there would be contained in appearance only.
    """
    root = temp_root()
    repo = Path(repo_root).resolve()
    wt = Path(worktree).resolve()

    if not wt.is_relative_to(root):
        raise VerifyEnvironmentError(
            f"verification worktree {wt} is not under the temp root {root}: the "
            f"sandbox would not make it writable, so the child could not run "
            f"tests. Refusing to launch.")
    if repo.is_relative_to(root):
        raise VerifyEnvironmentError(
            f"lead repository {repo} is inside the temp root {root}, which the "
            f"Bash sandbox makes WRITABLE. A VERIFY child there would be able to "
            f"write into the lead worktree — measured, not theoretical. Refusing "
            f"to launch an uncontained VERIFY child. Move the repository outside "
            f"{root}.")
    if repo == wt or wt.is_relative_to(repo):
        raise VerifyEnvironmentError(
            f"verification worktree {wt} is inside the lead repository {repo}: "
            f"side effects would land in the lead tree. Refusing to launch.")


def worktree_path(delegation_id: str, base: Path | None = None) -> Path:
    """Where this delegation's verification worktree lives.

    Under the temp root because that is the writable side of the sandbox boundary,
    and named by delegation so two concurrent reviews cannot collide or clean up
    each other's tree.
    """
    root = Path(base).resolve() if base else temp_root()
    return root / "ats-verify" / (delegation_id or "unknown")[:8]


def create(repo_root: str | Path, delegation_id: str, *,
           base: Path | None = None) -> VerifyEnv:
    """A worktree holding the lead's ACTUAL current state, plus a proof of it."""
    repo = Path(repo_root).resolve()
    if not (repo / ".git").exists():
        raise VerifyEnvironmentError(
            f"{repo} is not a git repository root, so there is no revision to "
            f"review and no way to reproduce the lead's result. Refusing.")

    wt = worktree_path(delegation_id, base)
    assert_contained(wt, repo)

    base_commit = _git(repo, "rev-parse", "HEAD").strip()
    subject = _git(repo, "log", "-1", "--format=%s").strip()
    diff = _git(repo, "diff", "HEAD")
    untracked = tuple(p for p in _git(
        repo, "ls-files", "--others", "--exclude-standard").split("\n") if p.strip())
    changed = tuple(p for p in _git(
        repo, "diff", "HEAD", "--name-only").split("\n") if p.strip())

    if wt.exists():
        remove(repo, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", "--quiet", str(wt), base_commit)

    try:
        if diff.strip():
            proc = subprocess.run(["git", "-C", str(wt), "apply", "--whitespace=nowarn", "-"],
                                  input=diff, capture_output=True, text=True)
            if proc.returncode != 0:
                raise VerifyEnvironmentError(
                    f"the lead's uncommitted diff does not apply cleanly to "
                    f"{base_commit[:8]}: {proc.stderr.strip()}. Refusing rather "
                    f"than reviewing a partially reproduced result.")
        for rel in untracked:
            src, dst = repo / rel, wt / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    except Exception:
        remove(repo, wt)
        raise

    manifest = _manifest(repo, wt, base_commit, subject, diff, untracked, changed)
    (wt / MANIFEST_NAME).write_text(manifest)
    return VerifyEnv(path=wt, repo_root=repo, base_commit=base_commit,
                     manifest_text=manifest, diff_sha256=_sha256(diff.encode()),
                     untracked=untracked, changed_files=changed)


def _manifest(repo: Path, wt: Path, base_commit: str, subject: str, diff: str,
              untracked: tuple[str, ...], changed: tuple[str, ...]) -> str:
    """What source state this worktree holds, stated so the child can check it.

    The per-file hash comparison is the load-bearing part: it is what turns "you
    are reviewing the lead's actual result" from a claim into something the child
    can verify for itself, and what would expose a patch that applied but drifted.
    """
    lines = [
        "ATS VERIFY ENVIRONMENT — what you are reviewing",
        "=" * 62,
        f"lead repository   : {repo}",
        f"base commit       : {base_commit}",
        f"base subject      : {subject}",
        f"worktree          : {wt}  (detached, disposable)",
        f"uncommitted diff  : sha256 {_sha256(diff.encode())[:32]}"
        f"  ({len(changed)} file(s), {len(diff.encode())} bytes)",
        f"untracked copied  : {', '.join(untracked) if untracked else '(none)'}",
        "",
        "EQUIVALENCE — lead file vs this worktree, sha256:",
    ]
    mismatched = []
    for rel in sorted(set(changed) | set(untracked)):
        a, b = repo / rel, wt / rel
        ha = _sha256(a.read_bytes()) if a.is_file() else "(absent)"
        hb = _sha256(b.read_bytes()) if b.is_file() else "(absent)"
        ok = ha == hb
        if not ok:
            mismatched.append(rel)
        lines.append(f"  {'MATCH   ' if ok else 'MISMATCH'} {rel}  {ha[:16]}")
    if not (changed or untracked):
        lines.append("  (lead tree is clean; you are reviewing the commit itself)")
    lines += [
        "",
        f"VERDICT: {'every reviewed file matches the lead' if not mismatched else 'MISMATCH in ' + ', '.join(mismatched)}",
        "",
        "This worktree is WRITABLE and disposable. Everything outside it, the lead",
        "repository included, is read-only to your shell at the kernel level. Run",
        "tests here freely; a commit here advances no branch and is discarded.",
        "=" * 62,
    ]
    return "\n".join(lines) + "\n"


def remove(repo_root: str | Path, worktree: str | Path) -> bool:
    """Deterministic teardown. Never raises — cleanup must not mask a result.

    Force is correct here and only here: the tree is disposable by construction
    and any commit in it is detached, so there is nothing a reader could lose.
    """
    repo, wt = Path(repo_root), Path(worktree)
    ok = True
    try:
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
    except Exception:  # noqa: BLE001
        ok = False
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
        ok = not wt.exists()
    try:
        _git(repo, "worktree", "prune", check=False)
    except Exception:  # noqa: BLE001
        pass
    return ok


def is_verify_worktree(path: str | Path) -> bool:
    """Whether `path` is one of ours, used to keep teardown from touching a
    worktree somebody else created."""
    p = Path(path).resolve()
    return (temp_root() / "ats-verify") in p.parents or p.parent.name == "ats-verify"
