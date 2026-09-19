#!/usr/bin/env python3
"""PreToolUse hook: ATS lock READ-GUARD — the missing half of coordination.

The PostToolUse presence hook BROADCASTS what I edit. Before an edit, this hook
reads live ATS locks: another session's exclusive lock blocks, an advisory lock
warns, and my own live lock is required inside coordinated repos. Session scope
records intent but never grants or blocks an edit (#2813).

Fail-OPEN: any error (server down, bad payload, no lock data) exits 0 and lets
the edit proceed — coordination must never wedge real work. Set
ATS_LOCKCHECK_BLOCK=0 to downgrade from block to warn-only.

Wire (~/.claude/settings.json):
  "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit|NotebookEdit",
    "hooks": [{ "type": "command",
      "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_lockcheck" }] }]
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys

from ai_team_sync.git_utils import resolve_repo_roots as _roots

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_SKIP_SUBSTR = ("/.git/", "/node_modules/", "/__pycache__/", "/.venv/",
                "/scratchpad/", "/.claude/", "/.playwright-mcp/")
_SKIP_PREFIX = ("/tmp/", "/var/tmp/", "/private/tmp/")


def _is_noise(path: str) -> bool:
    if any(path.startswith(p) for p in _SKIP_PREFIX):
        return True
    return any(s in path for s in _SKIP_SUBSTR)


def _rel(path: str, worktree_root: str | None = None) -> str:
    root = worktree_root if worktree_root is not None else _roots(path)[0]
    if root:
        try:
            return os.path.relpath(path, root)
        except Exception:
            pass
    return os.path.basename(path)


def normalize_pattern(pattern: str, repo_root: str = "") -> str:
    """A scope pattern as a repo-RELATIVE pattern.

    ATS scope is repo-relative, but nothing said so at the point of writing and
    nothing caught it afterwards, so an ABSOLUTE pattern
    ('/opt/anime-studio/packages/x.py') silently matched nothing: the guard
    compares against a rel built by _rel(), and
    fnmatch('packages/x.py', '/opt/anime-studio/packages/x.py') is False. The
    agent then reads "no lock or scope covering X" and takes a lock instead of
    fixing the scope — the exact loop observed 2026-08-23 (#2554).

    An absolute pattern under this repo is rewritten to its relative form. One
    that is absolute but NOT under this repo is left alone: it genuinely cannot
    describe a file here, and quietly re-rooting it would invent a claim.

    The lexical form comes from scope_paths.canonical_pattern, the one
    canonicalizer ATS uses server-side too, so 'src//a.py', 'src/./a.py' and a
    trailing '/' name the file they spell rather than nothing (#2741).
    """
    from ai_team_sync.scope_paths import canonical_pattern

    return canonical_pattern(pattern, repo_root)


def scope_matches(rel: str, pattern: str, repo_root: str = "") -> bool:
    """True if repo-relative `rel` falls under a scope `pattern`. Supports the
    '**' = any-depth convention ATS scopes use (e.g. 'packages/scene_generation/**').

    `repo_root` lets an absolute pattern be read as the relative one it meant;
    omitted, behaviour is exactly as before for relative patterns."""
    pat = normalize_pattern(pattern, repo_root)
    if not pat:
        return False
    if pat.endswith("/**"):
        base = pat[:-3]
        return rel == base or rel.startswith(base + "/")
    if pat.endswith("/*"):
        base = pat[:-2]
        return os.path.dirname(rel) == base
    return fnmatch.fnmatch(rel, pat)


def find_conflicts(rel: str, sessions: list, my_session_id: str,
                   file_repo_root: str = "", *, locks: list,
                   approved_lock_ids: set[str] | None = None,
                   my_ats_session_id: str = "") -> list:
    """OTHER sessions' live locks covering `rel`, with their modes.

    `locks` is the response from GET /api/locks, which filters out expired locks
    and dead owners. Exclude the exact ATS session when resolved; the Claude
    session label is only a fallback for legacy sessions.

    Repo anchoring (ats-lockcheck-repo-anchoring-p01): lock patterns are
    repo-RELATIVE, so a lock anchored to a DIFFERENT repo_root than the
    target file's git root cannot conflict — its 'tests/**' means ITS tests/.
    Either side unanchored ('') falls back to legacy match-everywhere."""
    mine = (my_session_id or "")[:8]
    froot = (file_repo_root or "").rstrip("/")
    owners = {str(s.get("id", "")): s for s in sessions or []}
    out = []
    for lk in locks:
        if str(lk.get("id", "")) in (approved_lock_ids or set()):
            continue
        s = owners.get(str(lk.get("session_id", "")))
        # A lock may arrive between the separate sessions and locks reads.
        # Its missing owner detail must not make a live exclusive lock vanish.
        agent = str(s.get("agent", "") if s else
                    (lk.get("agent") or lk.get("session_id", "")))
        if my_ats_session_id and str(lk.get("session_id", "")) == my_ats_session_id:
            continue
        if not my_ats_session_id and mine and mine in agent:
            continue
        sroot = str((s.get("repo_root") if s else lk.get("repo_root")) or "").rstrip("/")
        if froot and sroot and froot != sroot:
            continue                        # anchored to a different repo
        pat = str(lk.get("pattern", ""))
        if scope_matches(rel, pat, froot):
            out.append((agent, str(s.get("description", "") if s else "")[:90], pat,
                        str(lk.get("mode", ""))))
    return out


def _coordinated_roots() -> list[str]:
    # EMPTY by default. The claim guard fails CLOSED inside a coordinated repo,
    # so shipping someone else's repo paths as the default would either enforce
    # nothing (the paths do not exist) or enforce it somewhere unexpected.
    # Opt in per machine:
    #   export ATS_COORDINATED_REPOS=/srv/my-repo:/srv/other-repo
    raw = os.environ.get("ATS_COORDINATED_REPOS", "")
    return [r.rstrip("/") for r in raw.split(":") if r.strip()]


def claim_check(rel: str, froot: str, my_sid: str | None, my_cid8: str,
                sessions: list, locks: list) -> tuple[bool, str]:
    """(ok, reason) — do *I* hold a live claim covering `rel`? The CLAIM half
    of coordination (2026-08-17). find_conflicts asks "is someone ELSE here?";
    nothing asked "is my own session alive and does it claim this file?" — so
    a session reaped mid-turn (Stop-only heartbeats vs a 25-minute render
    turn) kept editing coordinated repos lock-less all day with zero warning.

    My session resolves pointer-first (concurrency-safe, Gap 3), falling back
    to agent-match on the hook payload's Claude session id. Only a live LOCK
    covering `rel` grants an edit; session scope is intent metadata. `locks`
    comes from GET /api/locks, which serves only live locks. Pure function of
    its inputs, for tests."""
    def _mine(s) -> bool:
        if my_sid:
            return str(s.get("id", "")) == my_sid
        return bool(my_cid8) and my_cid8 in str(s.get("agent", ""))

    mine_active = [s for s in sessions or []
                   if _mine(s) and str(s.get("status", "")).lower() == "active"]
    if not mine_active:
        mine_any = [s for s in sessions or [] if _mine(s)]
        if mine_any:
            return False, ("your ATS session was completed/reaped — its locks are "
                           "gone. Heartbeat/re-register (ats session start or POST "
                           "/api/sessions/<id>/heartbeat) and re-take locks before "
                           "editing this repo.")
        return False, ("no ATS session found for this agent — SessionStart "
                       "autostart did not register one. Run `ats session start` "
                       "with scope before editing this repo.")

    my_ids = {str(s.get("id", "")) for s in mine_active}
    for lk in locks or []:
        if str(lk.get("session_id", "")) not in my_ids:
            continue
        lroot = str(lk.get("repo_root") or "").rstrip("/")
        if lroot and froot and lroot != froot:
            continue
        if scope_matches(rel, str(lk.get("pattern", "")), froot):
            return True, ""
    # A refused restoration gives a more useful diagnostic than a generic
    # missing-lock message (#2760). The lock loop above already granted every
    # lane genuinely held. Scope itself never grants the edit (#2813).
    lost: list[str] = []
    for s in mine_active:
        sroot = str(s.get("repo_root") or "").rstrip("/")
        if sroot and froot and sroot != froot:
            continue
        not_restored = s.get("locks_not_restored") or []
        if isinstance(not_restored, str):
            not_restored = [not_restored]
        # ASK THE LANE ABOUT THE FILE, not the scope string about the lane.
        # Comparing spellings let a respelled scope entry walk past its own loss
        # ('src//**' is not the string 'src/**' but covers the same files), and
        # scope is caller-supplied text. Matching the lost lane against `rel`
        # uses the one comparison that is already authoritative for coverage, so
        # no spelling of scope can dodge a lane this session does not hold.
        covered_by_a_lost_lane = [
            str(lane) for lane in not_restored
            if scope_matches(rel, str(lane), froot)]
        if covered_by_a_lost_lane:
            lost.extend(covered_by_a_lost_lane)

    # Name the real reason before the generic ones: "you had this and lost it"
    # is a different instruction from "you never declared it".
    if lost:
        return False, (
            f"your ATS scope still names '{lost[0]}', but that lane was NOT restored "
            f"when this session was resurrected — another session took it while you "
            f"were reaped, or it expired. Scope is a declaration; the lock is the "
            f"claim. Check who holds it (ats lock check) and re-take it before "
            f"editing.")

    # Name an absolute scope pattern when present, while making clear that
    # correcting a declaration would still not grant an edit (#2554, #2813).
    near = []
    for s in mine_active:
        scope = s.get("scope") or []
        if isinstance(scope, str):
            scope = [scope]
        for pat in scope:
            pat = str(pat)
            if pat.startswith("/") and normalize_pattern(pat, froot) != pat:
                near.append((pat, normalize_pattern(pat, froot)))
    if near:
        abs_pat, rel_pat = near[0]
        return False, (
            f"your ATS scope pattern '{abs_pat}' is ABSOLUTE, but ATS scope is "
            f"repo-relative — declare it as '{rel_pat}' for intent tracking. "
            f"Your session needs a live lock on '{rel}' to edit.")
    return False, (f"your active ATS session holds no live lock covering "
                   f"'{rel}'. Take a lock first (ats lock / POST /api/locks). "
                   f"Declared scope records intent; it does not grant edits. "
                   f"Lock patterns are repo-relative, not absolute.")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable — never block
    if payload.get("tool_name") not in EDIT_TOOLS:
        sys.exit(0)
    fp = (payload.get("tool_input") or {}).get("file_path")
    if not fp or _is_noise(fp):
        sys.exit(0)
    wt_root, repo_root = _roots(fp)
    rel = _rel(fp, wt_root)

    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            session_response = client.get(f"{server}/api/sessions")
            session_response.raise_for_status()
            lock_response = client.get(f"{server}/api/locks")
            lock_response.raise_for_status()
            data = session_response.json()
            locks = lock_response.json()
    except Exception:
        sys.exit(0)  # server down / network — fail open
    sessions = data if isinstance(data, list) else data.get("sessions", data.get("data", []))
    locks = locks if isinstance(locks, list) else locks.get("locks", locks.get("data", []))

    froot = (repo_root or "").rstrip("/")
    # An approved override clears only the specific existing lock for this
    # session. Failed lookups leave the original conflict intact.
    approved_lock_ids: set[str] = set()
    my_sid = None
    try:
        from ai_team_sync import session_pointer as sp
        my_sid = sp.resolve_pointer(payload.get("session_id", ""), allow_global=False)
        if my_sid:
            with httpx.Client(timeout=2) as client:
                check = client.post(f"{server}/api/locks/check", json={
                    "paths": [rel], "repo_root": froot, "session_id": my_sid})
                check.raise_for_status()
                matches = check.json()[0].get("matches", [])
            approved_lock_ids = {m["lock_id"] for m in matches
                                 if m.get("override_granted") and not m.get("is_own")}
    except Exception:
        pass
    conflicts = find_conflicts(rel, sessions, payload.get("session_id", ""),
                               file_repo_root=froot, locks=locks,
                               approved_lock_ids=approved_lock_ids,
                               my_ats_session_id=my_sid or "")

    # In coordinated repos, explain a lost #2760 lane before a foreign lock's
    # diagnostic can hide it. The same claim guard still decides edit authority.
    if froot in _coordinated_roots():
        my_cid8 = str(payload.get("session_id", ""))[:8]
        ok, reason = claim_check(rel, froot, my_sid, my_cid8, sessions, locks)
        if not ok:
            print(f"ATS CLAIM GUARD: {reason}", file=sys.stderr)
            if os.environ.get("ATS_CLAIMCHECK", "1") != "0":
                sys.exit(2)

    warnings = []
    exclusive = [hit for hit in conflicts if hit[3] == "exclusive"]
    advisory = [hit for hit in conflicts if hit[3] != "exclusive"]
    if exclusive:
        lines = [f"ATS LOCK GUARD: '{rel}' is covered by another session's LIVE EXCLUSIVE lock:"]
        for agent, desc, pat, _mode in exclusive:
            lines.append(f"  - {agent}  [{pat}]  {desc}")
        lines.append("Coordinate with the holder or request override. "
                     "Set ATS_LOCKCHECK_BLOCK=0 to downgrade to warn-only.")
        warning = "\n".join(lines)
        print(warning, file=sys.stderr)
        if os.environ.get("ATS_LOCKCHECK_BLOCK", "2") != "0":
            sys.exit(2)
        warnings.append(warning)
    if advisory:
        lines = [f"ATS LOCK GUARD: '{rel}' is covered by another session's LIVE ADVISORY lock:"]
        for agent, desc, pat, _mode in advisory:
            lines.append(f"  - {agent}  [{pat}]  {desc}")
        warning = "\n".join(lines)
        print(warning, file=sys.stderr)
        warnings.append(warning)

    if warnings:
        # Claude Code consumes successful PreToolUse JSON on stdout. Exit-0
        # stderr is debug-only, so send every nonblocking lock warning into
        # the editing agent's context without changing permission handling.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n\n".join(warnings),
        }}))

    sys.exit(0)


if __name__ == "__main__":
    main()
