"""Idempotent installer for ATS git hooks (#3581 part B).

Where hooks go: `git rev-parse --git-path hooks`, which resolves to the COMMON
git dir's hooks/ from any linked worktree (hooks are shared by every worktree of
a repository) and honours core.hooksPath. Installing once therefore covers the
main checkout and all of its worktrees.

Layout per hook <name>:

  <name>-ats            ATS runner: `exec <python> -m ai_team_sync.hooks.<mod> "$@"`.
                        Rewritten on every install (python path refresh).
  <name>                The hook git runs. Created by us when absent.

When a hook of that name already exists and is not ours:

  guard hooks (pre-commit, pre-merge-commit, prepare-commit-msg)
      the existing hook is moved, unchanged, to <name>.ats-chained and <name>
      becomes a wrapper that runs it first and then the ATS runner; either
      failing fails the hook. Appending to the existing file instead would be
      silently ineffective whenever that file ends in `exit` -- the guard would
      never run.
  post hooks (post-commit, post-checkout)
      git ignores their exit status, so the existing file is kept in place and
      a single call line is appended (the long-standing install-hooks.sh
      behaviour). A post hook that is not a sh/bash script is left untouched and
      reported: it is never clobbered, renamed or rewritten.

Idempotent: a hook file already carrying MARKER is not re-chained or
re-appended. Nothing is ever deleted except by uninstall(), which restores a
chained hook to its original name.
"""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "ai-team-sync"
CHAINED_SUFFIX = ".ats-chained"

MODULES = {
    "pre-commit": "ai_team_sync.hooks.pre_commit",
    "pre-merge-commit": "ai_team_sync.hooks.pre_merge_commit",
    "prepare-commit-msg": "ai_team_sync.hooks.prepare_commit_msg",
    "post-commit": "ai_team_sync.hooks.post_commit",
    "post-checkout": "ai_team_sync.hooks.post_checkout",
}
GUARD_HOOKS = ("pre-commit", "pre-merge-commit", "prepare-commit-msg")
POST_HOOKS = ("post-commit", "post-checkout")
DEFAULT_HOOKS = ("pre-commit", "pre-merge-commit")


class InstallError(RuntimeError):
    pass


@dataclass
class InstallReport:
    hooks_dir: str
    created: list[str] = field(default_factory=list)
    chained: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"hooks dir: {self.hooks_dir}"]
        for label, names in (("created", self.created), ("chained existing", self.chained),
                             ("appended to existing", self.appended),
                             ("already installed", self.unchanged)):
            if names:
                out.append(f"{label}: {', '.join(names)}")
        for name, why in self.skipped:
            out.append(f"SKIPPED {name}: {why}")
        return out


def hooks_dir_for(repo: str | os.PathLike) -> Path:
    """The hooks directory git actually runs for `repo` (common dir; honours
    core.hooksPath)."""
    out = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
        cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        raise InstallError(f"{repo} is not a git repository: {out.stderr.strip()}")
    return Path(out.stdout.strip())


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _runner_text(hook: str, python: str) -> str:
    return (f"#!/bin/sh\n# {MARKER} runner for {hook}; managed by `ats hooks install`\n"
            f"exec {shlex.quote(python)} -m {MODULES[hook]} \"$@\"\n")


def _wrapper_text(hook: str) -> str:
    runner = f'"$hookdir/{hook}-ats"'
    chained = f'"$hookdir/{hook}{CHAINED_SUFFIX}"'
    if hook in POST_HOOKS:
        call_chained = f'  {chained} "$@"\n'
        call_runner = f'{runner} "$@"\nexit 0\n'
    else:
        call_chained = f'  {chained} "$@" || exit $?\n'
        call_runner = f'exec {runner} "$@"\n'
    return ("#!/bin/sh\n"
            f"# {MARKER} hook wrapper for {hook}; managed by `ats hooks install`.\n"
            f"# A pre-existing {hook} is preserved as {hook}{CHAINED_SUFFIX} and runs first.\n"
            'hookdir=$(cd "$(dirname "$0")" && pwd)\n'
            f"if [ -x {chained} ]; then\n{call_chained}fi\n"
            f"{call_runner}")


def _is_shell_script(text: str) -> bool:
    first = text.splitlines()[0] if text else ""
    if not first.startswith("#!"):
        return False
    interp = first[2:].strip().split()
    if not interp:
        return False
    name = os.path.basename(interp[0])
    if name == "env" and len(interp) > 1:
        name = os.path.basename(interp[1])
    return name in ("sh", "bash", "dash")


def install(repo: str | os.PathLike, hooks: list[str] | tuple[str, ...] = DEFAULT_HOOKS,
            python: str | None = None) -> InstallReport:
    unknown = [h for h in hooks if h not in MODULES]
    if unknown:
        raise InstallError(f"unknown hook(s) {unknown}; supported: {sorted(MODULES)}")
    python = python or sys.executable
    hdir = hooks_dir_for(repo)
    hdir.mkdir(parents=True, exist_ok=True)
    report = InstallReport(hooks_dir=str(hdir))

    for hook in hooks:
        hook_file = hdir / hook
        chained = hdir / f"{hook}{CHAINED_SUFFIX}"
        runner = hdir / f"{hook}-ats"

        if hook_file.exists() or hook_file.is_symlink():
            try:
                text = hook_file.read_text(errors="replace")
            except OSError as exc:
                report.skipped.append((hook, f"cannot read existing hook: {exc}"))
                continue
            if MARKER in text:
                _write_exec(runner, _runner_text(hook, python))
                report.unchanged.append(hook)
                continue
            if hook in POST_HOOKS:
                if not _is_shell_script(text):
                    report.skipped.append((
                        hook, "existing hook is not a sh/bash script; left untouched "
                              "(post hooks are never renamed or rewritten)"))
                    continue
                _write_exec(runner, _runner_text(hook, python))
                with hook_file.open("a") as fh:
                    fh.write(f'\n# {MARKER} hook\n"{runner}" "$@"\n')
                report.appended.append(hook)
                continue
            if chained.exists() or chained.is_symlink():
                report.skipped.append((
                    hook, f"{chained.name} already exists and {hook} is not ours; "
                          f"refusing to overwrite either -- resolve by hand"))
                continue
            os.rename(hook_file, chained)
            _write_exec(runner, _runner_text(hook, python))
            _write_exec(hook_file, _wrapper_text(hook))
            report.chained.append(hook)
            continue

        _write_exec(runner, _runner_text(hook, python))
        _write_exec(hook_file, _wrapper_text(hook))
        report.created.append(hook)
    return report


def uninstall(repo: str | os.PathLike, hooks: list[str] | tuple[str, ...] = tuple(MODULES)
              ) -> list[str]:
    """Remove what install() added; restore chained hooks. Returns actions."""
    hdir = hooks_dir_for(repo)
    actions: list[str] = []
    for hook in hooks:
        hook_file = hdir / hook
        chained = hdir / f"{hook}{CHAINED_SUFFIX}"
        runner = hdir / f"{hook}-ats"
        if hook_file.exists():
            text = hook_file.read_text(errors="replace")
            if text.startswith("#!/bin/sh\n# " + MARKER + " hook wrapper"):
                hook_file.unlink()
                actions.append(f"removed {hook}")
                if chained.exists():
                    os.rename(chained, hook_file)
                    actions.append(f"restored {hook} from {chained.name}")
            elif MARKER in text:
                kept = []
                lines = text.splitlines(keepends=True)
                skip_next = False
                for line in lines:
                    if line.strip() == f"# {MARKER} hook":
                        skip_next = True
                        continue
                    if skip_next and f"{hook}-ats" in line:
                        skip_next = False
                        continue
                    skip_next = False
                    kept.append(line)
                hook_file.write_text("".join(kept).rstrip("\n") + "\n")
                actions.append(f"removed ATS call line from {hook}")
        if runner.exists():
            runner.unlink()
            actions.append(f"removed {runner.name}")
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_team_sync.git_hook_install",
        description="Install ATS git hooks into a repository's common hooks dir.")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--hook", action="append", dest="hooks",
                        help=f"hook to install (repeatable); default {' '.join(DEFAULT_HOOKS)}")
    parser.add_argument("--python", default=None,
                        help="interpreter the hooks run (default: this one)")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.uninstall:
            for line in uninstall(args.repo, tuple(args.hooks or MODULES)):
                print(line)
            return 0
        report = install(args.repo, tuple(args.hooks or DEFAULT_HOOKS), args.python)
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for line in report.lines():
        print(line)
    return 2 if report.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
