"""Deterministic request-to-governed-context resolution.

This module decides only whether a prompt names a context ATS must resolve.  It
does not guess task authority: exact ticket syntax is parsed by the same helper
the brief builder uses, while project context is limited to repositories the
operator explicitly listed in ``ATS_COORDINATED_REPOS``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ai_team_sync.briefs import explicit_task_id
from ai_team_sync.git_utils import resolve_repo_roots


@dataclass(frozen=True)
class RequestTarget:
    task_id: int | None
    repo_root: str
    reason: str


def governed_roots() -> list[str]:
    """Operator-configured repositories, canonicalized without requiring them."""
    raw = os.environ.get("ATS_COORDINATED_REPOS", "")
    roots: list[str] = []
    for value in raw.split(":"):
        value = value.strip()
        if value:
            root = os.path.realpath(value).rstrip("/") or "/"
            if root not in roots:
                roots.append(root)
    return roots


def _normal_words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _aliases(root: str) -> set[str]:
    name = _normal_words(os.path.basename(root.rstrip("/")))
    aliases = {name} if name else set()
    # Tower's service repos commonly carry an infrastructure prefix while users
    # name the product without it ("Echo Brain", not "tower-echo-brain").
    if name.startswith("tower ") and len(name.split()) > 2:
        aliases.add(name.removeprefix("tower "))
    return aliases


def _repo_from_prompt(prompt: str, roots: list[str]) -> str:
    words = f" {_normal_words(prompt)} "
    matches = [root for root in roots if any(f" {alias} " in words for alias in _aliases(root))]
    return matches[0] if len(matches) == 1 else ""


def _repo_from_cwd(cwd: str, roots: list[str]) -> str:
    if not cwd:
        return ""
    current = os.path.realpath(cwd).rstrip("/") or "/"
    direct = [
        root for root in roots if current == root or current.startswith(root.rstrip("/") + "/")
    ]
    if len(direct) == 1:
        return direct[0]

    # Linked worktrees live outside the configured root.  Their .git file names
    # the shared checkout, which is the same anchor ATS lock readers use.
    _worktree, shared = resolve_repo_roots(os.path.join(current, ".ats-context"))
    shared = os.path.realpath(shared).rstrip("/") if shared else ""
    matches = [root for root in roots if shared == root]
    return matches[0] if len(matches) == 1 else ""


def resolve_request_target(
    prompt: str, *, cwd: str, governed_roots: list[str] | None = None
) -> RequestTarget | None:
    """Resolve the four bootstrap cases without fabricating generic scope."""
    roots = governed_roots if governed_roots is not None else globals()["governed_roots"]()
    roots = [os.path.realpath(root).rstrip("/") or "/" for root in roots]
    task_id = explicit_task_id(prompt)
    prompt_repo = _repo_from_prompt(prompt, roots)
    cwd_repo = _repo_from_cwd(cwd, roots)
    if task_id is not None:
        return RequestTarget(task_id, prompt_repo or cwd_repo, "explicit_task")
    if prompt_repo:
        return RequestTarget(None, prompt_repo, "project_name")
    if cwd_repo:
        return RequestTarget(None, cwd_repo, "governed_cwd")
    return None
