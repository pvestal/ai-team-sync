"""Worker identity, capability and authority — the control-plane half of ATS.

ATS owns work, authority and state; the models are workers. A worker is not a
model name, it is a *class of worker* with declared capabilities and declared
authority, and the two are separate questions: a local model may be perfectly
capable of proposing a patch while having no authority to commit one.

Why this lives on the server. The guard that protects a claimed scope runs as a
Claude Code PreToolUse hook. Codex has no hook mechanism, and a local worker has
no client at all, so a client-side rule is advice for everyone except Claude.
Authority is checked here, where every worker meets it on the same terms.

Operator ruling 2026-09-12: unmatched identities, including legacy 'unknown'
clients, fail closed to restricted authority. Explicit 'default' remains a
registered internal class; an unclassified label is never an explicit default.

Classification is not identity (#2741). A label SELECTS a class; for most
classes nothing proves the caller is entitled to it, so their authority is
coordination policy, not access control. A class that declares `bind_users` /
`bind_uids` is different: it is granted to a session only when the kernel says
the connection belongs to one of those accounts (see peer_identity), and only
identity-bound sessions can receive an ATS mutation grant. Because a
misconfigured binding would silently become a naming convention again, the
registry file is parsed strictly and a rejected file disables every bound class.
"""

from __future__ import annotations

import logging
import os
import pwd
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# edit: "none" | "claimed_scope"      — may it write files, and only inside its claim
# commit: bool                        — may it author commits
# land: bool                          — may it promote a commit onto a deployed
#   branch. Separate from commit: producing a change is not authority to ship it.
#   No builtin holds it.
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
    # Explicit internal authority class only. Unmatched identities NEVER land
    # here, including pre-registry clients labelled 'unknown'. Historical
    # compatibility cannot grant write authority to an unclassified identity.
    # Unbound, like every builtin: a worker that wants to claim this name can,
    # which is why no unbound class ever receives a mutation grant.
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

_SPEC_KEYS = frozenset({"capabilities", "authority", "cost_class", "concurrency",
                        "bind_users", "bind_uids"})
_AUTHORITY_KEYS = frozenset({"edit", "commit", "land", "task_close"})
_EDIT_VALUES = ("none", "claimed_scope")
_CLOSE_VALUES = ("no", "conditional", "yes")


class WorkerConfigError(ValueError):
    """A registry entry that cannot be given exactly one meaning."""


@dataclass(frozen=True)
class Authority:
    edit: str = "none"
    commit: bool = False
    task_close: str = "no"
    land: bool = False


@dataclass(frozen=True)
class Worker:
    name: str
    capabilities: tuple[str, ...]
    authority: Authority
    cost_class: str = "unknown"
    concurrency: int | None = None
    # OS accounts this class is bound to. Empty = unbound: the label alone
    # selects the class, and the class never receives a mutation grant.
    bind_uids: tuple[int, ...] = ()

    @property
    def identity_bound(self) -> bool:
        return bool(self.bind_uids)

    @property
    def may_claim_scope(self) -> bool:
        """Claiming an edit scope is a write claim — read-only workers do not."""
        return self.authority.edit != "none"

    @property
    def may_commit(self) -> bool:
        return self.authority.commit is True

    @property
    def may_close_task(self) -> bool:
        """Unconditional closing authority. 'conditional' is not a yes; it means
        the acceptance evidence decides, so it is answered at the gate, not here."""
        return self.authority.task_close == "yes"

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "worker": self.name,
            "capabilities": list(self.capabilities),
            "authority": {
                "edit": self.authority.edit,
                "commit": self.authority.commit,
                "land": self.authority.land,
                "task_close": self.authority.task_close,
            },
            "cost_class": self.cost_class,
            "concurrency": self.concurrency,
            "identity_bound": self.identity_bound,
        }
        if self.bind_uids:
            out["bind_uids"] = list(self.bind_uids)
        return out


class WorkerRegistry:
    def __init__(self, entries: dict[str, dict[str, Any]], *,
                 config_error: str | None = None):
        self._workers = {name: _build(name, spec) for name, spec in entries.items()}
        for required in ("default", "restricted"):
            if required not in self._workers:
                self._workers[required] = _build(required, _BUILTINS[required])
        _check_bound_families(self._workers)
        # Unmatched labels get fixed least privilege even if an operator config
        # overrides the explicit restricted class. No permissive opt-out flag.
        self._unregistered = _build("restricted", _BUILTINS["restricted"])
        self._unregistered_seen: set[str] = set()
        # Set when the operator's file was rejected and builtins are in force.
        # Builtins bind nobody, so while this is set no mutation can be granted.
        self.config_error = config_error

    def names(self) -> list[str]:
        return sorted(self._workers)

    def bound_account_uids(self) -> frozenset[int]:
        """Every OS account some class is bound to: the headless accounts."""
        return frozenset(uid for w in self._workers.values() for uid in w.bind_uids)

    def all(self) -> list[Worker]:
        return [self._workers[n] for n in self.names()]

    def registered(self, label: str | None) -> Worker | None:
        """An explicitly registered class or its deterministic suffix family.

        Labels carry an instance suffix ('claude-code:fb0bb6bf') and families
        carry a model suffix ('local:qwen3-30b'), so strip one ':'-segment at a
        time until something matches. Unknown roots never become a known class.
        """
        key = (label or "").strip()
        while key:
            if key in self._workers:
                return self._workers[key]
            if ":" not in key:
                break
            key = key.rsplit(":", 1)[0]
        return None

    def resolve(self, label: str | None) -> Worker:
        """Resolve a LABEL to its class. Says nothing about who is asking; code
        deciding what a session may do uses resolve_for_session."""
        worker = self.registered(label)
        if worker is not None:
            return worker

        if label and label not in self._unregistered_seen:
            self._unregistered_seen.add(label)
            logger.info("unregistered worker %r -> restricted", label)
        return self._unregistered

    def resolve_at_create(self, label: str | None, peer_uid: int | None
                          ) -> tuple[Worker, int | None, str | None]:
        """Identity established once, when a session is created.

        Returns (worker, bound_uid, refusal). An unbound class resolves as
        before with bound_uid None. A bound class is granted only to a peer the
        kernel reports as one of its accounts; any other peer, including one
        that cannot be identified, gets restricted and the reason.
        """
        worker = self.resolve(label)
        if not worker.bind_uids:
            return worker, None, None
        if peer_uid is not None and peer_uid in worker.bind_uids:
            return worker, peer_uid, None
        owner = "could not be identified" if peer_uid is None else f"is uid {peer_uid}"
        reason = (f"worker '{worker.name}' is bound to OS uid(s) {list(worker.bind_uids)} "
                  f"and this connection's owner {owner}")
        logger.warning("identity binding refused %r: %s", label, reason)
        return self._unregistered, None, reason

    def resolve_for_session(self, session: Any) -> tuple[Worker, bool, str | None]:
        """The class that governs an EXISTING session, from what was recorded
        at creation. Returns (worker, identity_bound, note).

        The label is never re-trusted on its own: a session whose label names a
        bound class but which was not bound at creation is restricted, and a
        recorded binding that the current registry no longer supports (class
        removed, rebound or unbound) is restricted too.
        """
        worker = self.resolve(getattr(session, "agent", None))
        bound_worker = getattr(session, "bound_worker", "") or ""
        bound_uid = getattr(session, "bound_uid", None)
        if not worker.bind_uids:
            if bound_worker:
                return (self._unregistered, False,
                        f"session was bound to '{bound_worker}', which the registry no "
                        f"longer binds to an OS account")
            return worker, False, None
        if bound_worker == worker.name and bound_uid is not None and bound_uid in worker.bind_uids:
            return worker, True, None
        return (self._unregistered, False,
                f"label names identity-bound worker '{worker.name}' but this session "
                f"was not bound to it when it was created")


def _strict_bool(name: str, key: str, value: Any) -> bool:
    if type(value) is not bool:
        raise WorkerConfigError(
            f"worker {name!r}: authority.{key} must be true or false, not {value!r}")
    return value


def _choice(name: str, key: str, value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise WorkerConfigError(
            f"worker {name!r}: authority.{key} must be one of {list(allowed)}, not {value!r}")
    return value


def _bind_uids(name: str, spec: dict[str, Any]) -> tuple[int, ...]:
    raw_uids = spec.get("bind_uids", [])
    raw_users = spec.get("bind_users", [])
    if not isinstance(raw_uids, list):
        raise WorkerConfigError(f"worker {name!r}: bind_uids must be a list of integers")
    if not isinstance(raw_users, list):
        raise WorkerConfigError(f"worker {name!r}: bind_users must be a list of account names")
    uids: set[int] = set()
    for uid in raw_uids:
        if type(uid) is not int or uid < 0:
            raise WorkerConfigError(f"worker {name!r}: bind_uids entry {uid!r} is not a uid")
        uids.add(uid)
    for user in raw_users:
        if not isinstance(user, str) or not user.strip():
            raise WorkerConfigError(f"worker {name!r}: bind_users entry {user!r} is not an account name")
        try:
            uids.add(pwd.getpwnam(user).pw_uid)
        except KeyError:
            raise WorkerConfigError(
                f"worker {name!r}: bind_users names unknown OS account {user!r}") from None
    if 0 in uids:
        raise WorkerConfigError(f"worker {name!r}: binding to root distinguishes nobody")
    if os.getuid() in uids:
        raise WorkerConfigError(
            f"worker {name!r}: binding to uid {os.getuid()}, the account ATS itself runs as, "
            f"distinguishes nobody — every interactive client on this host runs as it")
    return tuple(sorted(uids))


def _build(name: str, spec: dict[str, Any]) -> Worker:
    if not isinstance(name, str) or not name or name != name.strip():
        raise WorkerConfigError(f"worker name {name!r} is not a clean label")
    if not isinstance(spec, dict):
        raise WorkerConfigError(f"worker {name!r}: entry must be a table")
    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        raise WorkerConfigError(f"worker {name!r}: unknown key(s) {sorted(unknown)}")
    auth = spec.get("authority", {})
    if not isinstance(auth, dict):
        raise WorkerConfigError(f"worker {name!r}: authority must be a table")
    unknown = set(auth) - _AUTHORITY_KEYS
    if unknown:
        raise WorkerConfigError(f"worker {name!r}: unknown authority key(s) {sorted(unknown)}")
    capabilities = spec.get("capabilities", ["repo_read"])
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise WorkerConfigError(f"worker {name!r}: capabilities must be a list of strings")
    cost_class = spec.get("cost_class", "unknown")
    if not isinstance(cost_class, str):
        raise WorkerConfigError(f"worker {name!r}: cost_class must be a string")
    concurrency = spec.get("concurrency")
    if concurrency is not None and (type(concurrency) is not int or concurrency < 1):
        raise WorkerConfigError(f"worker {name!r}: concurrency must be a positive integer")
    return Worker(
        name=name,
        capabilities=tuple(capabilities or ["repo_read"]),
        authority=Authority(
            edit=_choice(name, "edit", auth.get("edit", "none"), _EDIT_VALUES),
            commit=_strict_bool(name, "commit", auth.get("commit", False)),
            task_close=_choice(name, "task_close", auth.get("task_close", "no"), _CLOSE_VALUES),
            land=_strict_bool(name, "land", auth.get("land", False)),
        ),
        cost_class=cost_class,
        concurrency=concurrency,
        bind_uids=_bind_uids(name, spec),
    )


def _check_bound_families(workers: dict[str, Worker]) -> None:
    """A class under a bound family must be bound to a subset of its accounts.

    Otherwise 'echo-executor:x' registered without a binding would be the way
    around 'echo-executor' being bound: the longest registered prefix wins.
    """
    for name, worker in workers.items():
        key = name
        while ":" in key:
            key = key.rsplit(":", 1)[0]
            parent = workers.get(key)
            if parent is not None and parent.bind_uids and not (
                    worker.bind_uids and set(worker.bind_uids) <= set(parent.bind_uids)):
                raise WorkerConfigError(
                    f"worker {name!r} sits under identity-bound {key!r} and must be bound "
                    f"to a subset of its accounts")


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

    A file that cannot be parsed or validated is rejected WHOLE, never in part.
    Coordination must not wedge, so the builtins stay in force — but builtins
    bind nobody, so while the file is rejected ATS grants no mutation at all.
    """
    builtins: dict[str, dict[str, Any]] = {k: dict(v) for k, v in _BUILTINS.items()}
    path = _config_path()
    if not path:
        return WorkerRegistry(builtins)
    try:
        with open(path, "rb") as fh:
            document = tomllib.load(fh)
        unknown = set(document) - {"workers"}
        if unknown:
            raise WorkerConfigError(f"unknown top-level key(s) {sorted(unknown)}")
        loaded = document.get("workers", {})
        if not isinstance(loaded, dict):
            raise WorkerConfigError("[workers] must be a table")
        entries = dict(builtins)
        for name, spec in loaded.items():
            if not isinstance(spec, dict):
                raise WorkerConfigError(f"worker {name!r}: entry must be a table")
            entries[name] = spec
        return WorkerRegistry(entries)
    except Exception as exc:  # noqa: BLE001 — never wedge; never partially trust
        logger.error("worker registry %s REJECTED (%s); builtins only, no identity-bound "
                     "class in force", path, exc)
        return WorkerRegistry(builtins, config_error=f"{path}: {exc}")
