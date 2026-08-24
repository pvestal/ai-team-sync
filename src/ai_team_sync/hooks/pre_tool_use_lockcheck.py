#!/usr/bin/env python3
"""PreToolUse hook: ATS lock READ-GUARD — the missing half of coordination.

The PostToolUse presence hook BROADCASTS what I edit. Nothing made an agent
READ who owns a file before editing it, so "do not clobber" was purely manual
and silently failed (a parallel session's locked file could be overwritten with
zero warning). This closes that: before an Edit/Write/MultiEdit, it asks the ATS
server which OTHER active sessions declared scope over the target file, and
BLOCKS the edit (exit 2, reason on stderr) when one does — excluding my own
session via the hook payload's session_id so I never block myself.

Fail-OPEN: any error (server down, bad payload, no scope data) exits 0 and lets
the edit proceed — coordination must never wedge real work. Set
ATS_LOCKCHECK_BLOCK=0 to downgrade from block to warn-only.

Wire (~/.claude/settings.json):
  "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit|NotebookEdit",
    "hooks": [{ "type": "command",
      "command": "<ats-venv>/bin/python <this-file>" }] }]
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_SKIP_SUBSTR = ("/.git/", "/node_modules/", "/__pycache__/", "/.venv/",
                "/scratchpad/", "/.claude/", "/.playwright-mcp/")
_SKIP_PREFIX = ("/tmp/", "/var/tmp/", "/private/tmp/")


def _is_noise(path: str) -> bool:
    if any(path.startswith(p) for p in _SKIP_PREFIX):
        return True
    return any(s in path for s in _SKIP_SUBSTR)


def _git_root(path: str) -> str | None:
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _rel(path: str) -> str:
    root = _git_root(path)
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
    """
    pat = (pattern or "").strip().rstrip("/")
    if not pat.startswith("/"):
        return pat
    root = (repo_root or "").rstrip("/")
    if root and (pat == root or pat.startswith(root + "/")):
        return pat[len(root):].lstrip("/")
    return pat


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
                   file_repo_root: str = "") -> list:
    """OTHER active sessions whose scope covers `rel`. Excludes my own session
    (matched by the session-id prefix the server appends to the agent id).

    Repo anchoring (ats-lockcheck-repo-anchoring-p01): scope patterns are
    repo-RELATIVE, so a session anchored to a DIFFERENT repo_root than the
    target file's git root cannot conflict — its 'tests/**' means ITS tests/.
    Either side unanchored ('') falls back to legacy match-everywhere."""
    mine = (my_session_id or "")[:8]
    froot = (file_repo_root or "").rstrip("/")
    out = []
    for s in sessions or []:
        if str(s.get("status", "")).lower() != "active":
            continue
        agent = str(s.get("agent", ""))
        if mine and mine in agent:          # my own session — never self-block
            continue
        sroot = str(s.get("repo_root") or "").rstrip("/")
        if froot and sroot and froot != sroot:
            continue                        # anchored to a different repo
        scope = s.get("scope") or s.get("files") or []
        if isinstance(scope, str):
            scope = [scope]
        for pat in scope:
            if scope_matches(rel, str(pat), froot):
                out.append((agent, str(s.get("description", ""))[:90], str(pat)))
                break
    return out


def _coordinated_roots() -> list[str]:
    raw = os.environ.get("ATS_COORDINATED_REPOS",
                         "/opt/anime-studio:/opt/tower-echo-brain")
    return [r.rstrip("/") for r in raw.split(":") if r.strip()]


def claim_check(rel: str, froot: str, my_sid: str | None, my_cid8: str,
                sessions: list, locks: list) -> tuple[bool, str]:
    """(ok, reason) — do *I* hold a live claim covering `rel`? The CLAIM half
    of coordination (2026-08-17). find_conflicts asks "is someone ELSE here?";
    nothing asked "is my own session alive and does it claim this file?" — so
    a session reaped mid-turn (Stop-only heartbeats vs a 25-minute render
    turn) kept editing coordinated repos lock-less all day with zero warning.

    My session resolves pointer-first (concurrency-safe, Gap 3), falling back
    to agent-match on the hook payload's Claude session id. A claim is a LOCK
    whose pattern covers `rel` (locks are the primary claim primitive) or a
    session SCOPE pattern covering it, either anchored to this repo or
    unanchored. Pure function of its inputs, for tests."""
    def _mine(s) -> bool:
        if my_sid and str(s.get("id", "")) == my_sid:
            return True
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
    for s in mine_active:
        sroot = str(s.get("repo_root") or "").rstrip("/")
        if sroot and froot and sroot != froot:
            continue
        scope = s.get("scope") or []
        if isinstance(scope, str):
            scope = [scope]
        for pat in scope:
            if scope_matches(rel, str(pat), froot):
                return True, ""

    # Say WHY, not just no. An absolute pattern that would have matched once
    # read as repo-relative is the single most common way this guard fires on
    # someone who believed they had declared scope, so name it and give the
    # exact replacement instead of sending them to take a lock (#2554).
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
            f"repo-relative — it matches nothing. Re-declare it as '{rel_pat}' "
            f"(ats scope / PATCH /api/sessions/<id>), or take a lock on "
            f"'{rel}'.")
    return False, (f"your active ATS session holds no lock or scope covering "
                   f"'{rel}'. Take a lock first (ats lock / POST /api/locks) — "
                   f"claims are what stop two sessions clobbering one file. "
                   f"Patterns are repo-relative, not absolute.")


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
    rel = _rel(fp)

    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            data = client.get(f"{server}/api/sessions").json()
    except Exception:
        sys.exit(0)  # server down / network — fail open
    sessions = data if isinstance(data, list) else data.get("sessions", data.get("data", []))

    froot = (_git_root(fp) or "").rstrip("/")
    conflicts = find_conflicts(rel, sessions, payload.get("session_id", ""),
                               file_repo_root=froot)

    # Claim guard — only inside coordinated repos, and only when the server
    # ANSWERED (the fail-open above already exited on server errors): being
    # unclaimed there is exactly the silent-clobber hole, so it fails CLOSED.
    # ATS_CLAIMCHECK=0 downgrades to warn-only.
    if not conflicts and froot in _coordinated_roots():
        try:
            import httpx
            with httpx.Client(timeout=2) as client:
                locks = client.get(f"{server}/api/locks").json()
            if not isinstance(locks, list):
                locks = locks.get("locks", locks.get("data", []))
        except Exception:
            sys.exit(0)  # locks endpoint unreachable — fail open
        my_sid = None
        try:
            from ai_team_sync import session_pointer as sp
            my_sid = sp.resolve_pointer()
        except Exception:
            pass
        my_cid8 = str(payload.get("session_id", ""))[:8]
        ok, reason = claim_check(rel, froot, my_sid, my_cid8, sessions, locks)
        if ok:
            sys.exit(0)
        print(f"ATS CLAIM GUARD: {reason}", file=sys.stderr)
        sys.exit(2 if os.environ.get("ATS_CLAIMCHECK", "1") != "0" else 0)

    if not conflicts:
        sys.exit(0)

    lines = [f"ATS LOCK GUARD: '{rel}' is inside another ACTIVE session's scope — coordinate, do not clobber:"]
    for agent, desc, pat in conflicts:
        lines.append(f"  - {agent}  [{pat}]  {desc}")
    lines.append("Read their work (ats / :8400) and coordinate, or request override. "
                 "Set ATS_LOCKCHECK_BLOCK=0 to downgrade to warn-only.")
    print("\n".join(lines), file=sys.stderr)
    sys.exit(2 if os.environ.get("ATS_LOCKCHECK_BLOCK", "2") != "0" else 0)


if __name__ == "__main__":
    main()
