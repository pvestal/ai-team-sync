"""Deterministic request-to-governed-context resolution.

This module decides only whether a prompt names a context ATS must resolve.  It
does not guess task authority: exact ticket syntax is parsed by the same helper
the brief builder uses, while project context is limited to repositories the
operator explicitly listed in the shared ATS operator configuration.

A project named in prompt text is context routing only (operator ruling
2026-10-14). It never switches or blocks a session that is already anchored;
switching is the structured start_session(repo_root=...) action, and project
authority is enforced at the operation guards (edit, Bash, git, restart).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ai_team_sync.briefs import explicit_task_ids
from ai_team_sync.git_utils import resolve_repo_roots
from ai_team_sync.operator_config import (
    context_aliases, context_repositories, governed_repositories)

_HANDBACK_PREFIX = '<agent-message from="'
_HANDBACK_MARKER = "[Subagent hand-back]"


def is_subagent_handback(prompt: str) -> bool:
    """True when the harness delivered a subagent's report as the prompt.

    The report names whatever repos the subagent touched; that is not the
    operator switching projects. Only a prompt that opens with the harness
    frame counts, so an operator pasting a report still resolves by its text.
    """
    return prompt.startswith(_HANDBACK_PREFIX) and _HANDBACK_MARKER in prompt[:500]


@dataclass(frozen=True)
class RequestTarget:
    task_id: int | None
    repo_root: str
    reason: str
    # Where the signals came from, so the hook can tell the worker's physical
    # location (cwd) apart from names that merely appear in the prompt text.
    mentioned: tuple[str, ...] = ()
    cwd_repo: str = ""
    # Every explicitly named ticket when there is more than one (#3591): a
    # prompt naming #3536 and #3560 gets both, never neither.
    task_ids: tuple[int, ...] = ()
    # The repository is listed for context only: brief it, never anchor or
    # enforce on it (#3591).
    context_only: bool = False
    # Context this resolver did not provide before #3591 (several tickets,
    # several projects, context-only repositories). If ATS cannot answer, the
    # prompt proceeds with a note instead of being blocked: added context must
    # never add fail-closed exposure (operator ruling 2026-10-14).
    best_effort: bool = False


def governed_roots() -> list[str]:
    """Machine-local operator-governed repositories shared by all clients."""
    return governed_repositories()


def context_roots() -> list[str]:
    """Context-only repositories: briefed, never governed (#3591)."""
    governed = set(governed_roots())
    return [root for root in context_repositories() if root not in governed]


def _normal_words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _aliases(root: str, extra: dict[str, list[str]] | None = None) -> set[str]:
    name = _normal_words(os.path.basename(root.rstrip("/")))
    aliases = {name} if name else set()
    # Tower's service repos commonly carry an infrastructure prefix while users
    # name the product without it ("Echo Brain", not "tower-echo-brain").
    if name.startswith("tower ") and len(name.split()) > 2:
        aliases.add(name.removeprefix("tower "))
    # Short names ("ATS", "POV") cannot be derived safely from a directory name;
    # the operator lists them (#3591).
    aliases.update(_normal_words(a) for a in (extra or {}).get(root, []) if _normal_words(a))
    return aliases


def _mentioned(prompt: str, roots: list[str],
               extra: dict[str, list[str]] | None = None) -> list[str]:
    words = f" {_normal_words(prompt)} "
    return [root for root in roots
            if any(f" {alias} " in words for alias in _aliases(root, extra))]


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
    prompt: str, *, cwd: str, governed_roots: list[str] | None = None,
    context_roots: list[str] | None = None,
) -> RequestTarget | None:
    """Resolve the bootstrap cases without fabricating generic scope.

    Returns None only for a genuinely generic prompt. Several named tickets,
    several named projects and context-only repositories each resolve to
    context rather than to nothing (#3591): the most cross-cutting requests
    were the ones that used to receive no context at all.
    """
    if is_subagent_handback(prompt):
        # Never anchors (#3591); the hook briefs it only for an anchored
        # session, and a subagent's report never blocks its parent's turn.
        return RequestTarget(None, "", "subagent_handback", best_effort=True)
    governed = governed_roots if governed_roots is not None else globals()["governed_roots"]()
    governed = [os.path.realpath(root).rstrip("/") or "/" for root in governed]
    context = context_roots if context_roots is not None else globals()["context_roots"]()
    context = [os.path.realpath(r).rstrip("/") or "/" for r in context if r not in governed]
    roots = governed + context
    ids = explicit_task_ids(prompt)
    mentioned = _mentioned(prompt, roots, context_aliases())
    prompt_repo = mentioned[0] if len(mentioned) == 1 else ""
    cwd_repo = _repo_from_cwd(cwd, roots)
    repo = prompt_repo or cwd_repo
    # Exactly the pre-#3591 rule for "governed" (fail-closed): one explicit
    # ticket, one governed repository named, or a governed working directory.
    named_governed = _mentioned(prompt, governed)
    legacy_governed = (len(ids) == 1 or len(named_governed) == 1
                       or bool(_repo_from_cwd(cwd, governed)))
    signals = dict(mentioned=tuple(mentioned), cwd_repo=cwd_repo,
                   context_only=bool(repo) and repo in context,
                   best_effort=not legacy_governed)
    if len(ids) == 1:
        return RequestTarget(ids[0], repo, "explicit_task", **signals)
    if len(ids) > 1:
        return RequestTarget(None, repo, "explicit_tasks", task_ids=tuple(ids), **signals)
    if prompt_repo:
        return RequestTarget(None, prompt_repo, "project_name", **signals)
    if len(mentioned) > 1:
        # No single repository; still project work. Briefed without an anchor.
        return RequestTarget(None, cwd_repo, "multi_project", **signals)
    if cwd_repo:
        return RequestTarget(None, cwd_repo,
                             "context_cwd" if signals["context_only"] else "governed_cwd",
                             **signals)
    return None
