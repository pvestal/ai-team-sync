"""Worker identity, capability and authority — the control-plane half of ATS.

ATS owns work, authority and state; the models are workers. A worker is not a
model name, it is a *class of worker* with declared capabilities and declared
authority, and the two are separate questions: a local model may be perfectly
capable of proposing a patch while having no authority to commit one.

Why this lives on the server. The guard that protects a claimed scope runs as a
Claude Code PreToolUse hook. Codex has no hook mechanism, and a local worker has
no client at all, so a client-side rule is advice for everyone except Claude.
Authority is checked here, where every worker meets it on the same terms.

An unregistered worker keeps the pre-registry rights and is logged by name, so
adding this registry cannot break a client that predates it. Set
ATS_STRICT_WORKERS=1 to drop unregistered workers to read-only once the fleet is
registered — that is a fleet migration, not a default.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# edit: "none" | "claimed_scope"      — may it write files, and only inside its claim
# commit: bool                        — may it author commits
# task_close: "no" | "conditional" | "yes"
#   conditional = may close only against satisfied acceptance evidence, never on
#   its own say-so. Closing is an authority act, not an implementation act.
#   Conditional is held by the frontier workers as a CLASS, not per model: there
#   is one conditional path and both read the same value through it.
_BUILTINS: dict[str, dict[str, Any]] = {
    "claude-code": {
        "capabilities": ["repo_read", "multi_file_edit", "architecture",
                         "debugging", "tests"],
        "authority": {"edit": "claimed_scope", "commit": True,
                      "task_close": "conditional"},
        "cost_class": "cloud",
        "concurrency": None,
    },
    # Peer frontier worker to claude-code for Tower coding work, so it holds the
    # SAME conditional close authority (operator ruling 2026-09-12). Withholding
    # it made the lead-worker lifecycle unprovable by anyone but Claude, which is
    # a property of the client rather than of the work. Not unconditional, and
    # not broader autonomy: the acceptance evidence still decides at the gate.
    "codex": {
        "capabilities": ["repo_read", "bounded_edit", "tests", "code_review"],
        "authority": {"edit": "claimed_scope", "commit": True,
                      "task_close": "conditional"},
        "cost_class": "cloud",
        "concurrency": None,
    },
    # Prefix family: 'local:qwen3-30b', 'local:gpt-oss-20b', ... all land here
    # unless a file registers that exact model. Findings yes, verdicts no — the
    # standing rule that a local model's pass/fail means nothing against the
    # operator's bar is expressed as authority, not as etiquette.
    "local": {
        "capabilities": ["repo_read", "log_triage", "classify", "summarize",
                         "failure_cluster", "plan_review", "retrieval"],
        "authority": {"edit": "none", "commit": False, "task_close": "no"},
        "cost_class": "local",
        "concurrency": 1,
    },
    # Where an unregistered label lands. Deny-by-default is the right END state
    # and the wrong DEFAULT today: the fleet already posts labels this registry
    # has never heard of (the VS Code extension and older CLI builds send
    # agent="unknown", tests and scripts send their own), and refusing them would
    # break working clients to enforce a rule none of them has been told about.
    # So an unregistered worker keeps the pre-registry rights and gets logged by
    # name, which is how you find out what to register. Flip
    # ATS_STRICT_WORKERS=1 once the fleet is registered and unregistered drops to
    # 'restricted' below.
    #
    # None of this is access control. The API is unauthenticated by design, so a
    # worker that wants to claim another's name can. It stops a worker exceeding
    # its role by ACCIDENT, and it makes roles discoverable and declared.
    "default": {
        "capabilities": ["repo_read", "multi_file_edit", "tests"],
        "authority": {"edit": "claimed_scope", "commit": True, "task_close": "no"},
        "cost_class": "unknown",
        "concurrency": None,
    },
    "restricted": {
        "capabilities": ["repo_read"],
        "authority": {"edit": "none", "commit": False, "task_close": "no"},
        "cost_class": "unknown",
        "concurrency": 1,
    },
}


@dataclass(frozen=True)
class Authority:
    edit: str = "none"
    commit: bool = False
    task_close: str = "no"


@dataclass(frozen=True)
class Worker:
    name: str
    capabilities: tuple[str, ...]
    authority: Authority
    cost_class: str = "unknown"
    concurrency: int | None = None

    @property
    def may_claim_scope(self) -> bool:
        """Claiming an edit scope is a write claim — read-only workers do not."""
        return self.authority.edit != "none"

    @property
    def may_commit(self) -> bool:
        return bool(self.authority.commit)

    @property
    def may_close_task(self) -> bool:
        """Unconditional closing authority. 'conditional' is not a yes; it means
        the acceptance evidence decides, so it is answered at the gate, not here."""
        return self.authority.task_close == "yes"

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.name,
            "capabilities": list(self.capabilities),
            "authority": {
                "edit": self.authority.edit,
                "commit": self.authority.commit,
                "task_close": self.authority.task_close,
            },
            "cost_class": self.cost_class,
            "concurrency": self.concurrency,
        }


class WorkerRegistry:
    def __init__(self, entries: dict[str, dict[str, Any]]):
        self._workers = {name: _build(name, spec) for name, spec in entries.items()}
        for required in ("default", "restricted"):
            if required not in self._workers:
                self._workers[required] = _build(required, _BUILTINS[required])
        self._strict = (os.environ.get("ATS_STRICT_WORKERS") or "").strip() in ("1", "true", "yes")
        self._unregistered_seen: set[str] = set()

    def names(self) -> list[str]:
        return sorted(self._workers)

    def all(self) -> list[Worker]:
        return [self._workers[n] for n in self.names()]

    def resolve(self, label: str | None) -> Worker:
        """A session label to the worker that governs it.

        Labels carry an instance suffix ('claude-code:fb0bb6bf') and families
        carry a model suffix ('local:qwen3-30b'), so strip one ':'-segment at a
        time until something matches. No match is 'default', never 'trusted'.
        """
        key = (label or "").strip()
        while key:
            if key in self._workers:
                return self._workers[key]
            if ":" not in key:
                break
            key = key.rsplit(":", 1)[0]

        if label and label not in self._unregistered_seen:
            self._unregistered_seen.add(label)
            logger.info("unregistered worker %r -> %s", label,
                        "restricted" if self._strict else "default")
        return self._workers["restricted" if self._strict else "default"]


def _build(name: str, spec: dict[str, Any]) -> Worker:
    auth = dict(spec.get("authority") or {})
    return Worker(
        name=name,
        capabilities=tuple(spec.get("capabilities") or ["repo_read"]),
        authority=Authority(
            edit=str(auth.get("edit", "none")),
            commit=bool(auth.get("commit", False)),
            task_close=str(auth.get("task_close", "no")),
        ),
        cost_class=str(spec.get("cost_class", "unknown")),
        concurrency=spec.get("concurrency"),
    )


def _config_path() -> Path | None:
    explicit = (os.environ.get("ATS_WORKERS_CONFIG") or "").strip()
    if explicit:
        return Path(explicit)
    default = Path.home() / ".config" / "ai-team-sync" / "workers.toml"
    return default if default.exists() else None


@lru_cache(maxsize=1)
def registry() -> WorkerRegistry:
    """The live registry: builtins, overridden and extended by an optional TOML.

    Cached — this is read on the session-create path. Call registry.cache_clear()
    after editing the file (tests do; the server picks it up on restart).
    """
    entries: dict[str, dict[str, Any]] = {k: dict(v) for k, v in _BUILTINS.items()}
    path = _config_path()
    if path:
        try:
            with open(path, "rb") as fh:
                loaded = tomllib.load(fh).get("workers") or {}
            for name, spec in loaded.items():
                if isinstance(spec, dict):
                    entries[name] = spec
        except Exception as exc:  # noqa: BLE001 — never wedge on a bad config
            logger.warning("worker registry %s ignored (%s); using builtins", path, exc)
    return WorkerRegistry(entries)
