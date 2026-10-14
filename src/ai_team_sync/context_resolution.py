"""Deterministic request-to-governed-context resolution.

This module decides only whether a prompt names a context ATS must resolve.  It
does not guess task authority: exact ticket syntax is parsed by the same helper
the brief builder uses, while project context is limited to repositories the
operator explicitly listed in the shared ATS operator configuration.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ai_team_sync.briefs import explicit_task_id
from ai_team_sync.git_utils import resolve_repo_roots
from ai_team_sync.operator_config import governed_repositories


@dataclass(frozen=True)
class RequestTarget:
    task_id: int | None
    repo_root: str
    reason: str
    # How the prompt text names governed repositories (see classify_mentions).
    # Only `directives` may move or block a session that already holds work.
    mentioned: tuple[str, ...] = ()
    topical: tuple[str, ...] = ()
    directives: tuple[tuple[str, str], ...] = ()
    cwd_repo: str = ""

    @property
    def directed(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(root for root, _clause in self.directives))


def governed_roots() -> list[str]:
    """Machine-local operator-governed repositories shared by all clients."""
    return governed_repositories()


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


# --------------------------------------------------------------------------- #
# Directive classification
#
# Naming a project is not asking to work in it. Incident 2026-10-14: "Do not
# change anime-studio" in a GO prompt was read as a switch and blocked the GO.
# A mention is a WORK directive only when, inside one clause of the operator's
# own text, it follows a leading work verb with no negation or subordinator in
# between. Quoted, fenced, example and harness-framed text is not the operator's
# instruction and is removed first. The rules are deliberately narrow: a missed
# directive still meets the edit guards, a false one blocks the operator.
# --------------------------------------------------------------------------- #

_WORK_VERBS = frozenset(
    "add apply ask build cd change clean commit configure continue create debug delete "
    "deploy disable dispatch do downgrade edit enable finish fix generate go implement "
    "install merge migrate modify move open patch push rebase refactor release remove "
    "rename render repair replace restart resume revert rewrite rollback run ship start "
    "switch train tune update upgrade work write".split()
)
_SUBJECT_VERBS = frozenset(
    "audit check describe explain find give inspect investigate list look read report "
    "review show summarise summarize tell".split()
)
# Leading words that do not change what the clause asks for.
_FILLERS = frozenset(
    "please now then next also instead correction ok okay so first second third finally "
    "and just kindly immediately right lets let s us i we you need needs want to "
    "should must thanks thank".split()
)
# A modal is a polite request only before "you" ("could you fix X"); "I would
# build X" is a recommendation, not an instruction.
_MODALS = frozenset("can could would will".split())
_NEGATIONS = frozenset(
    "not no never without avoid except excluding nor neither nothing none stop".split()
)
_CONTRACTED_NEGATIONS = frozenset(
    "don doesn didn isn aren wasn weren won wouldn can couldn shouldn mustn haven hasn".split()
)
# Between the verb and the mention these end the verb's reach: "start a new
# session before switching ... to anime-studio" does not direct work there.
_SUBORDINATORS = frozenset(
    "before after until unless rather instead than while when whenever if whether "
    "because since so that which who whose where once though although like ahead "
    "behind".split()
)

_NON_INSTRUCTION = [
    # Harness frames carry reports and tool output, not operator direction.
    re.compile(
        r"<(agent-message|system-reminder|task-notification|local-command-stdout|"
        r"local-command-stderr|bash-stdout|bash-stderr|tool-result)\b[^>]*>.*?</\1\s*>",
        re.S | re.I,
    ),
    re.compile(r"(```|~~~).*?(\1|\Z)", re.S),           # fenced code
    re.compile(r"`[^`\n]*`"),                             # inline code
    re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d'),   # double quotes
    re.compile(r"(?<![\w])'[^'\n]+'(?![\w])"),           # single quotes, not apostrophes
    re.compile(r"^[ \t]*>.*$", re.M),                     # blockquote lines
    re.compile(                                           # examples
        r"(?:\be\.g\.|\bi\.e\.|\b(?:for example|for instance|such as|examples?)\b)"
        r"[^.\n;!?]*",
        re.I,
    ),
]
_CLAUSE_SPLIT = re.compile(r"[\n;:,()\[\]{}|]+|[.!?]+(?=\s|$)|\s[-\u2013\u2014]+\s")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


_ABSOLUTE_PATH = re.compile(r"(?<![\w.~-])/[\w.@+-]+(?:/[\w.@+-]*)*")


def _operator_text(prompt: str, roots: list[str]) -> str:
    text = re.sub(r"</?pasted_content\b[^>]*>", "\n", prompt or "")
    for pattern in _NON_INSTRUCTION:
        text = pattern.sub(" ", text)

    # A path names a governed repository only when it lies inside one;
    # /home/x/anime-studio-authz merely shares the name.
    def _path(match: re.Match[str]) -> str:
        path = match.group(0).rstrip(".")
        inside = any(path == root or path.startswith(root + "/") for root in roots)
        return match.group(0) if inside else " "

    text = _ABSOLUTE_PATH.sub(_path, text)
    return text.replace("**", " ").replace("__", " ")


def _alias_positions(words: list[str], alias: str) -> list[int]:
    target = alias.split()
    width = len(target)
    return [i for i in range(len(words) - width + 1) if words[i : i + width] == target]


def _clause_level(words: list[str], mention: int) -> int:
    """2 = work directive, 1 = subject (read) request, 0 = incidental."""
    i = 0
    while i < mention:
        if words[i] in _FILLERS:
            i += 1
        elif words[i] in _MODALS and i + 1 < mention and words[i + 1] == "you":
            i += 2
        else:
            break
    if i >= mention:
        return 0
    verb = words[i]
    if verb in _WORK_VERBS:
        level = 2
    elif verb in _SUBJECT_VERBS:
        level = 1
    else:
        return 0
    for j in range(i + 1, mention):
        word = words[j]
        if word in _NEGATIONS or word in _SUBORDINATORS:
            return 0
        if word == "t" and words[j - 1] in _CONTRACTED_NEGATIONS:
            return 0
    return level


def classify_mentions(
    prompt: str, roots: list[str]
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """(mentioned, topical, directives) for the governed roots a prompt names.

    `mentioned` is every root named anywhere (the long-standing rule, used for
    first anchoring). `topical` adds read requests ("give me the X status").
    `directives` pairs each root with the clause that instructs work there.
    """
    raw = f" {_normal_words(prompt)} "
    mentioned = [r for r in roots if any(f" {a} " in raw for a in _aliases(r))]
    best: dict[str, int] = {}
    evidence: dict[str, str] = {}
    for clause in _CLAUSE_SPLIT.split(_operator_text(prompt, roots)):
        clause = _LIST_MARKER.sub("", clause).strip()
        words = _normal_words(clause).split()
        if not words:
            continue
        for root in roots:
            for alias in _aliases(root):
                for position in _alias_positions(words, alias):
                    level = _clause_level(words, position)
                    if level > best.get(root, 0):
                        best[root] = level
                        evidence[root] = clause
    topical = [r for r in roots if best.get(r, 0) >= 1]
    directives = [(r, evidence[r]) for r in roots if best.get(r, 0) == 2]
    return mentioned, topical, directives


def _unique(values: list[str]) -> str:
    return values[0] if len(values) == 1 else ""


def resolve_request_target(
    prompt: str, *, cwd: str, governed_roots: list[str] | None = None
) -> RequestTarget | None:
    """Resolve the four bootstrap cases without fabricating generic scope."""
    roots = governed_roots if governed_roots is not None else globals()["governed_roots"]()
    roots = [os.path.realpath(root).rstrip("/") or "/" for root in roots]
    task_id = explicit_task_id(prompt)
    mentioned, topical, directives = classify_mentions(prompt, roots)
    directed = list(dict.fromkeys(root for root, _clause in directives))
    cwd_repo = _repo_from_cwd(cwd, roots)
    # The strongest unambiguous signal names the prompt's repository. A bare
    # mention never outranks the repository the worker is standing in.
    if directed:
        prompt_repo = _unique(directed)
    elif topical:
        prompt_repo = _unique(topical)
    else:
        prompt_repo = "" if cwd_repo else _unique(mentioned)
    evidence = dict(
        mentioned=tuple(mentioned),
        topical=tuple(topical),
        directives=tuple(directives),
        cwd_repo=cwd_repo,
    )
    if task_id is not None:
        return RequestTarget(task_id, prompt_repo or cwd_repo, "explicit_task", **evidence)
    if prompt_repo:
        return RequestTarget(None, prompt_repo, "project_name", **evidence)
    if cwd_repo:
        return RequestTarget(None, cwd_repo, "governed_cwd", **evidence)
    if directed:
        # Work directed at several governed repositories at once: the hook must
        # still see it so an anchored session can refuse the switch.
        return RequestTarget(None, "", "project_directives", **evidence)
    return None
