"""Shared core of the ATS git-hook guards (pre-commit, pre-merge-commit) and the
Bash PreToolUse write guard (#3581 part B).

WHAT THIS LAYER IS. The Claude PreToolUse edit guard (pre_tool_use_lockcheck)
only sees Edit/Write/MultiEdit/NotebookEdit. Shell writes and whole-tree git
operations reach the working tree without it: on 2026-10-13 a `git merge`
changed five files another live session had claimed. These guards ask ATS the
same question at two more choke points -- the commit, and the Bash command --
using the server's own lock reader, POST /api/locks/check.

WHAT IT IS NOT. A git hook runs only when git runs it: `--no-verify`, a
fast-forward merge, cherry-pick/revert/rebase picks, `git am`, and any write
that never becomes a commit are outside it. docs/commit-guard.md lists every
layer's coverage and its residual bypasses. Nothing here claims to cover every
filesystem write.

THE RULE, per resolved path inside a GOVERNED repository
(operator_config.governed_repositories):

* the committing session is identified  -> another live session's lock of ANY
  mode covering the path refuses;
* no session can be identified (a human) -> only another session's EXCLUSIVE
  lock refuses; advisory holders are printed;
* ATS unreachable                        -> refuse (fail closed).

Outside governed repositories the legacy behaviour stays: exclusive refuses,
advisory warns, ATS unreachable allows.

An approved override (override_granted) clears only that lock, exactly as the
edit guard does.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

OVERRIDE_ENV = "ATS_COMMIT_GUARD_OVERRIDE"
AUDIT_LOG_ENV = "ATS_COMMIT_GUARD_AUDIT_LOG"
TAG = "[ats-commit-guard]"


class GuardUnavailable(RuntimeError):
    """ATS could not be asked, so no verdict exists."""


# --- repository placement -------------------------------------------------

def _git(args: list[str], cwd: str | os.PathLike | None = None,
         check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=check)


def repo_roots(cwd: str | os.PathLike | None = None) -> tuple[str, str] | None:
    """(worktree toplevel, canonical repo root) for the checkout containing `cwd`.

    The canonical root is the MAIN checkout, derived from
    `git rev-parse --git-common-dir`: a linked worktree's common dir is
    <main>/.git, so sessions working in any worktree of a project anchor to one
    root -- the one ATS stores as session.repo_root and the operator lists as
    governed. None outside a work tree (or for a bare repository).
    """
    out = _git(["rev-parse", "--path-format=absolute", "--show-toplevel",
                "--git-common-dir"], cwd=cwd)
    if out.returncode != 0:
        return None
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    top, common = (os.path.realpath(line.strip()) for line in lines)
    if os.path.basename(common) == ".git":
        root = os.path.dirname(common)
    else:
        # A separate git dir (`git init --separate-git-dir`) or a bare repo with a
        # work tree: the toplevel is the only checkout path there is.
        root = top
    return top, root


def governed(root: str) -> bool:
    """Whether `root` is operator-governed. An unreadable operator config raises
    OperatorConfigError: the callers refuse rather than guess."""
    from ai_team_sync.operator_config import governed_repositories

    return os.path.realpath(root).rstrip("/") in governed_repositories()


# --- who is asking ----------------------------------------------------------

@dataclass
class Identity:
    """Who the guard believes is acting. COOPERATIVE, NOT AUTHENTICATED: every
    source below is a value the acting process (or another process of the same
    OS account) can set. See docs/commit-guard.md, "Identity"."""

    session_id: str | None
    source: str              # per_session | env | delegated | none
    conflict: str = ""       # non-empty: $ATS_SESSION_ID contradicts the pointer

    @property
    def self_asserted(self) -> bool:
        """An identity that came only from the environment / a capability file,
        with no per-Claude-session pointer behind it."""
        return self.source in ("env", "delegated")


def resolve_identity(cid: str | None = None) -> Identity:
    """The acting ATS session, local sources only (#3581 review finding 1).

    An identity REMOVES that session's locks from the verdict, so a process
    that inherits or sets another session's id -- the lock holder's -- would
    pass. Order therefore differs from session_pointer.resolve_pointer, where
    $ATS_SESSION_ID always wins:

    1. When a Claude session id is known (the hook payload's, else
       $CLAUDE_CODE_SESSION_ID / the live-cid record) and its per-session
       pointer ~/.ats_session_<cid8> exists, the POINTER wins. A different
       $ATS_SESSION_ID is a conflict: the caller refuses and names both.
    2. Otherwise $ATS_SESSION_ID, then a delegated child's capability file --
       both self-asserted, so the caller must validate them with
       identity_problem() before trusting them.
    3. The shared ~/.ats_session is never used: it names whichever session on
       the box wrote it last.
    """
    from ai_team_sync import session_pointer as sp

    env = (os.environ.get("ATS_SESSION_ID") or "").strip()
    cid = (cid or sp.claude_session_id() or "").strip()
    if cid:
        try:
            pointer = sp.session_pointer_path(cid).read_text().strip()
        except OSError:
            pointer = ""
        if pointer:
            conflict = ""
            if env and env != pointer:
                conflict = (f"$ATS_SESSION_ID={env} contradicts this Claude session's own "
                            f"ATS pointer ({cid[:8]} -> {pointer}). An inherited or "
                            f"borrowed session id would remove that session's locks from "
                            f"the verdict; unset ATS_SESSION_ID or fix the pointer.")
            return Identity(pointer, "per_session", conflict)
    if env:
        return Identity(env, "env")
    delegated = sp.load_delegated_capability()
    if delegated:
        return Identity(delegated[0], "delegated")
    return Identity(None, "none")


def identify_session() -> tuple[str | None, str]:
    """(session id, source) without validation; kept for callers that only
    display it. Guards use resolve_identity() + identity_problem()."""
    ident = resolve_identity()
    return ident.session_id, ident.source


def identity_problem(ident: Identity, results: list[dict], timeout: float = 5.0,
                     governed_repo: bool = False) -> str:
    """Why `ident` must not be trusted, or '' when it may be.

    Checks against existing endpoints:

    * ownership -- /api/locks/check, asked WITH this session id, sets
      caller_identity_unresolved when the server's #2741 boundary
      (routers/locks.cross_account: the kernel's uid for this loopback socket vs
      the session's creator_uid) refuses the claim, or the id is unknown;
    * liveness -- GET /api/sessions/<id> must say status 'active';
    * creator -- the same read's creator_uid must be this process's uid. A NULL
      creator_uid (a legacy row from before #2741, which cross_account accepts
      for an ordinary account) is unverified, and refused in a governed repo.

    Raises GuardUnavailable when the liveness read fails in transport."""
    sid = ident.session_id
    if not sid:
        return ""
    if any(r.get("caller_identity_unresolved") for r in results):
        return (f"ATS does not accept session {sid} as one owned by this OS account "
                f"(unknown id, or created by another account)")
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{server_url()}/api/sessions/{sid}")
    except Exception as exc:
        raise GuardUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code == 404:
        return f"session {sid} is not known to ATS"
    if resp.status_code != 200:
        raise GuardUnavailable(f"GET /api/sessions/{sid} returned {resp.status_code}")
    body = resp.json() or {}
    status = str(body.get("status", ""))
    if status != "active":
        return f"session {sid} is '{status}', not active"
    creator = body.get("creator_uid")
    if creator is None:
        if governed_repo:
            return (f"session {sid} is a legacy row with no recorded creator (pre-#2741), "
                    f"so its ownership cannot be verified")
    elif creator != os.getuid():
        return f"session {sid} was created by another OS account (uid {creator})"
    return ""


# --- asking ATS ---------------------------------------------------------------

def server_url() -> str:
    return os.environ.get("ATS_SERVER_URL", "http://localhost:8400").rstrip("/")


def check_paths(paths: list[str], repo_root: str, session_id: str | None,
                timeout: float = 5.0) -> list[dict]:
    """POST /api/locks/check for repo-relative `paths` placed in `repo_root`.

    Raises GuardUnavailable on any transport or protocol failure; never returns
    a partial answer."""
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{server_url()}/api/locks/check", json={
                "paths": paths, "repo_root": repo_root, "session_id": session_id or ""})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # transport, HTTP status, JSON
        raise GuardUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, list) or len(data) != len(paths):
        raise GuardUnavailable("unexpected /api/locks/check response shape")
    return data


@dataclass
class Holder:
    path: str
    session_id: str
    agent: str
    mode: str
    pattern: str
    reason: str = ""

    def line(self) -> str:
        why = f' -- "{self.reason}"' if self.reason else ""
        return (f"  {self.path} -- {self.mode} lock '{self.pattern}' held by "
                f"{self.agent} (session {self.session_id}){why}")


@dataclass
class Verdict:
    blocking: list[Holder] = field(default_factory=list)
    advisory: list[Holder] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking


def foreign_holders(results: list[dict], my_session_id: str | None,
                    my_agent_hint: str = "") -> list[Holder]:
    """Every OTHER session's lock in a /api/locks/check answer.

    Own locks are recognised by the server (is_own, which it grants only to a
    session the requesting OS account owns) or by exact session id. When no
    session id is known, `my_agent_hint` (a Claude session's cid8) is the same
    legacy label fallback the edit guard's find_conflicts uses. Locks with an
    approved override for this caller are not foreign obstacles."""
    out: list[Holder] = []
    for result in results:
        path = str(result.get("path", ""))
        for match in result.get("matches") or []:
            sid = str(match.get("session_id", ""))
            if match.get("is_own") or (my_session_id and sid == my_session_id):
                continue
            if not my_session_id and my_agent_hint and my_agent_hint in str(match.get("agent", "")):
                continue
            if match.get("override_granted"):
                continue
            out.append(Holder(path=path, session_id=sid, agent=str(match.get("agent", "")),
                              mode=str(match.get("mode", "")),
                              pattern=str(match.get("pattern", "")),
                              reason=str(match.get("reason", "") or "")))
    return out


def verdict(holders: list[Holder], *, governed_repo: bool, identified: bool) -> Verdict:
    """Apply the rule in the module docstring to foreign holders."""
    v = Verdict()
    for h in holders:
        if h.mode == "exclusive" or (governed_repo and identified):
            v.blocking.append(h)
        else:
            v.advisory.append(h)
    return v


# --- override audit -----------------------------------------------------------

def audit_log_path() -> Path:
    override = (os.environ.get(AUDIT_LOG_ENV) or "").strip()
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-team-sync" / "commit-guard-audit.log"


def record_override(*, hook: str, reason: str, repo_root: str, paths: list[str],
                    session_id: str | None, would_block: list[Holder] | None,
                    note: str = "") -> Path | None:
    """Append one JSON line per override. Returns the log path, or None when it
    could not be written (the override still proceeds; the warning says so)."""
    path = audit_log_path()
    try:
        user = getpass.getuser()
    except Exception:
        user = str(os.getuid())
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "hook": hook, "reason": reason, "repo_root": repo_root, "cwd": os.getcwd(),
        "user": user, "session_id": session_id or "", "paths": paths[:200],
        "would_block": [h.__dict__ for h in (would_block or [])],
        "note": note,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return path
    except OSError:
        return None


# --- the git-hook entry point -----------------------------------------------

def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def run_git_guard(hook: str, paths: list[str], cwd: str | None = None) -> int:
    """Shared body of pre-commit and pre-merge-commit. Returns the exit code."""
    from ai_team_sync.operator_config import OperatorConfigError

    paths = sorted({p for p in paths if p})
    if not paths:
        return 0
    roots = repo_roots(cwd)
    if roots is None:
        return 0
    _top, root = roots
    override = (os.environ.get(OVERRIDE_ENV) or "").strip()
    ident = resolve_identity()
    sid = ident.session_id

    if ident.conflict:
        if override:
            return _override(hook, override, root, paths, sid, None,
                             note=f"identity conflict: {ident.conflict}")
        _err(f"\n{TAG} REFUSED ({hook}): {ident.conflict}")
        return 1

    try:
        is_governed = governed(root)
    except OperatorConfigError as exc:
        if override:
            return _override(hook, override, root, paths, sid, None,
                             note=f"operator config invalid: {exc}")
        _err(f"\n{TAG} REFUSED ({hook}): the ATS operator config is invalid, so this "
             f"guard cannot tell whether {root} is governed:\n  {exc}\n"
             f"Fix ~/.config/ai-team-sync/operator.toml, or bypass with "
             f"{OVERRIDE_ENV}=<reason> (audited).")
        return 1

    try:
        results = check_paths(paths, root, sid)
        problem = identity_problem(ident, results, governed_repo=is_governed)
        if problem:
            if ident.self_asserted and is_governed and not override:
                _err(f"\n{TAG} REFUSED ({hook}) in governed repository {root}: the "
                     f"committing identity came from the environment "
                     f"(${'ATS_SESSION_ID' if ident.source == 'env' else 'delegated capability'}"
                     f") and is not valid: {problem}.\nUnset it, or commit from the "
                     f"session that owns it.")
                return 1
            # Not trusted: judge as an unidentified committer, with a fresh answer
            # (the invalid id may still have been honoured for is_own).
            _err(f"{TAG} note: ignoring session identity ({problem}); judging as an "
                 f"unidentified committer.")
            sid = None
            results = check_paths(paths, root, None)
    except GuardUnavailable as exc:
        if override:
            return _override(hook, override, root, paths, sid, None,
                             note=f"ATS unreachable: {exc}")
        if not is_governed:
            return 0  # legacy: coordination never wedges an ungoverned repo
        _err(f"\n{TAG} REFUSED ({hook}): {root} is an ATS-governed repository and the "
             f"ATS server at {server_url()} could not be asked ({exc}).\n"
             f"This guard fails closed. Start the server, or bypass with "
             f"{OVERRIDE_ENV}=<reason> (printed and audited) or "
             f"`git commit --no-verify` (unaudited).")
        return 1

    holders = foreign_holders(results, sid)
    v = verdict(holders, governed_repo=is_governed, identified=bool(sid))
    if override:
        return _override(hook, override, root, paths, sid, v.blocking)

    if v.advisory:
        _err(f"\n{TAG} WARNING ({hook}): these paths overlap another session's live "
             f"advisory lock:")
        for h in v.advisory:
            _err(h.line())
    if v.ok:
        return 0
    who = (f"session {sid}" if sid else
           "an unidentified committer (no ATS session; only EXCLUSIVE locks refuse)")
    _err(f"\n{TAG} REFUSED ({hook}) for {who} in {root}:")
    for h in v.blocking:
        _err(h.line())
    _err("Coordinate with the holder (ats lock check / request_override), or bypass "
         f"with {OVERRIDE_ENV}=<reason> (printed and audited).")
    return 1


def _override(hook: str, reason: str, root: str, paths: list[str], sid: str | None,
              blocking: list[Holder] | None, note: str = "") -> int:
    log = record_override(hook=hook, reason=reason, repo_root=root, paths=paths,
                          session_id=sid, would_block=blocking, note=note)
    _err(f"\n{TAG} OVERRIDE ({hook}) in {root}: {OVERRIDE_ENV}={reason!r}")
    for h in blocking or []:
        _err("  bypassed:" + h.line())
    if note:
        _err(f"  note: {note}")
    _err(f"  audit: {log}" if log else "  audit: COULD NOT WRITE the audit log")
    return 0


# --- path sets ------------------------------------------------------------------

def staged_paths(cwd: str | None = None) -> list[str]:
    """Index vs HEAD (vs the empty tree before the first commit), renames split
    into their delete and add halves: removing a claimed path is a change to it."""
    out = _git(["diff", "--cached", "--name-only", "--no-renames", "-z"], cwd=cwd)
    if out.returncode != 0:
        raise GuardUnavailable(f"git diff --cached failed: {out.stderr.strip()}")
    return [p for p in out.stdout.split("\0") if p]


def merge_heads(cwd: str | None = None) -> list[str]:
    """Commits named in MERGE_HEAD (several for an octopus). Present while a
    conflicted merge is being concluded by `git commit`; git 2.43 has NOT yet
    written it when pre-merge-commit runs (measured), so there the staged set
    is the merge's effect."""
    out = _git(["rev-parse", "--git-path", "MERGE_HEAD"], cwd=cwd)
    if out.returncode != 0:
        return []
    path = out.stdout.strip()
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    try:
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    except OSError:
        return []


def merge_incoming_paths(cwd: str | None = None) -> list[str]:
    """Paths the merged side(s) changed since the merge base (HEAD...MERGE_HEAD).
    Three-dot on purpose: two-dot would also list paths changed only on the
    current branch, which the merge does not touch."""
    paths: set[str] = set()
    for head in merge_heads(cwd):
        out = _git(["diff", "--name-only", "--no-renames", "-z", f"HEAD...{head}"], cwd=cwd)
        if out.returncode == 0:
            paths.update(p for p in out.stdout.split("\0") if p)
    return sorted(paths)
