"""MCP Server for ai-team-sync integration with Claude Code - Complete Edition."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent

from ai_team_sync.session_target import (ownership_refusal,
                                         resolve_completion_target)
from ai_team_sync.session_marker import (
    adopted_summary,
    derived_working_description,
    is_autoregistered,
)


# MCP Server instance
mcp_server = Server("ai-team-sync")

# Server URL from environment
SERVER_URL = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")

# Session file for persistence
# Honors $ATS_STATE_DIR, so a delegated child launched with its own state
# directory cannot read or write its parent's pointers.
def _session_file() -> Path:
    from ai_team_sync import session_pointer as sp
    return sp.global_pointer_path()


def get_git_user() -> str:
    """Get git user name."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def get_git_branch() -> str:
    """Get current git branch."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# Repo anchoring (ats-lockcheck-repo-anchoring-p01) is OPT-IN via the explicit
# repo_root tool argument. Deliberately NO cwd-derived default: on multi-repo
# boxes the MCP process cwd (~/Documents) is not the repo being locked, and a
# wrong anchor silently disables enforcement for the real repo (false negative
# — strictly worse than the cross-repo false positive this feature fixes).


def detect_agent() -> str:
    """Detect which AI agent is active.

    Honors ATS_AGENT (explicit override) first, then known env signatures.
    NOTE: Claude Code sets CLAUDECODE (no underscore) — the old CLAUDE_CODE-only
    check returned 'unknown', which is why concurrent sessions all showed as the
    git user with '(unknown)' (#1556). Mirrors cli._detect_agent.
    """
    from ai_team_sync import session_pointer as sp
    return sp.detect_agent()  # one detection (#2517)


def session_agent_label() -> str:
    """Per-session agent identity: base agent type + a short session token so two
    CONCURRENT sessions of the same agent are distinguishable (#1556) instead of
    collapsing to one 'claude-code'. Token from CLAUDE_CODE_SESSION_ID / ATS_SESSION.

    This server is spawned once per Claude process, so its own
    CLAUDE_CODE_SESSION_ID goes stale at the first /clear while every hook sees
    the rotated value. The lock guard self-excludes by string-matching the live
    hook cid against this label, so reading the environment directly made a
    session block its own edits (#2003). session_pointer.claude_session_id()
    prefers the cid the SessionStart hook published for this Claude process.
    """
    from ai_team_sync import session_pointer as sp

    return sp.agent_label(detect_agent())  # one label (#2517)


# Identity of the session THIS MCP process started. A stdio MCP server is
# spawned once per agent session and lives as long as it, so in-process memory
# is the one identity no other agent can write — the property every file-based
# pointer lacks. Codex in particular has no CLAUDE_CODE_SESSION_ID and so no
# per-session pointer file; without this it could only fall back to the shared
# global file, which is exactly how it completed a delegated child's row on
# 2026-09-11 while reporting that its own session had completed.
_IN_PROCESS_SESSION_ID: str | None = None


def resolve_identity() -> tuple[str | None, str]:
    """(session id, provenance) for this process.

    Goes through load_session_id() so the established injection seam still
    works, then asks the pointer layer WHERE that id lives. An id that matches
    no pointer file we can see did not come from shared state, so it is
    'explicit' — a caller handed it to us directly.
    """
    if _IN_PROCESS_SESSION_ID:
        return _IN_PROCESS_SESSION_ID, "in_process"

    sid = load_session_id()
    if not sid:
        return None, "none"
    try:
        from ai_team_sync import session_pointer as sp
        found, source = sp.resolve_pointer_source()
    except Exception:
        return sid, "explicit"
    return sid, (source if found == sid else "explicit")


def mutation_refusal(session_id: str | None, source: str,
                     row: dict | None, my_label: str) -> str | None:
    """Why this mutation must NOT proceed, or None to allow it.

    Pure, so the rule is testable without a server. Fails CLOSED: anything it
    cannot prove is refused rather than attempted against a row that may belong
    to somebody else.
    """
    if not session_id:
        return ("No active session for this process. Start one (start_session) "
                "or set ATS_SESSION_ID; refusing to guess which session to mutate.")

    if source == "global":
        return ("Refusing to mutate: this session id came from the SHARED "
                f"pointer file, which names whichever session wrote it last "
                f"({session_id}), not necessarily yours. Start a session in "
                "this process, or set ATS_SESSION_ID explicitly.")

    if source in ("in_process", "explicit"):
        # in_process: this process started it, nothing else can have written it.
        # explicit: it came from the caller, not from a file another agent shares.
        return None

    # env / per_session: the id is process-local, but a process can be handed
    # (or inherit) an id naming another worker's row. Binding is by exact id;
    # this check only rejects a row that is plainly not ours.
    if row is None:
        return (f"Refusing to mutate: session {session_id} was not found on "
                "the server, so ownership cannot be established.")
    agent = str(row.get("agent") or "")
    if agent and my_label and agent != my_label:
        return (f"Refusing to mutate: session {session_id} belongs to "
                f"{agent}, not to {my_label}.")
    return None


def save_session_id(session_id: str):
    """Save active session ID for persistence. Writes the legacy global file AND a
    per-session pointer keyed by CLAUDE_CODE_SESSION_ID so concurrent sessions
    don't clobber each other's pointer (Gap 3)."""
    _session_file().write_text(session_id)
    try:
        from ai_team_sync import session_pointer as sp
        sp.save_pointer(session_id)
    except Exception:
        pass  # back-compat: global file alone still works for a single session


def load_session_id() -> str | None:
    """Load THIS session's active ID. Prefers the per-session pointer (Gap 3) and
    falls back to the legacy global file."""
    try:
        from ai_team_sync import session_pointer as sp
        sid = sp.resolve_pointer()
        if sid:
            return sid
    except Exception:
        pass
    f = _session_file()
    if f.exists():
        content = f.read_text().strip()
        return content if content else None
    return None


def clear_session_id():
    """Clear saved session ID."""
    f = _session_file()
    if f.exists():
        f.unlink()


def format_conflict_guidance(conflicts: list[dict]) -> str:
    """Format conflict resolution guidance."""
    msg = "💡 **Resolution Options:**\n\n"

    has_exclusive = any(c.get("lock_mode") == "exclusive" for c in conflicts)

    if has_exclusive:
        msg += "**Option 1:** Request override permission\n"
        msg += "   Use: request_override tool with justification\n"
        msg += "   Keywords for auto-approval: urgent, security, hotfix, critical\n\n"

        msg += "**Option 2:** Coordinate with lock owner\n"
        msg += "   Use: team_status to see who's working\n"
        msg += "   Contact them to discuss coordination\n\n"

        msg += "**Option 3:** Work on different scope\n"
        msg += "   Adjust your scope patterns to avoid overlap\n\n"
    else:
        msg += "**Advisory locks detected** - you can proceed but should coordinate:\n"
        msg += "   1. Check team_status to see who's working\n"
        msg += "   2. Log your decisions with log_decision\n"
        msg += "   3. Communicate with team members\n\n"

    return msg


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        # Original 8 tools
        Tool(
            name="start_session",
            description="Start a new working session with scope locks. Announces your work to the team and creates locks to prevent conflicts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File patterns to lock (e.g., ['src/**', 'backend/auth/**'])",
                    },
                    "description": {
                        "type": "string",
                        "description": "What you're working on",
                    },
                    "exclusive": {
                        "type": "boolean",
                        "description": "If true, blocks all overlapping work (use for critical changes)",
                        "default": False,
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Absolute git root your scope patterns are relative to "
                                       "(e.g., '/srv/my-repo'). PASS THIS when working a "
                                       "specific repo: it anchors your locks so identical "
                                       "patterns in OTHER repos don't false-block anyone. "
                                       "Omit only for cross-repo/unscoped work (legacy "
                                       "match-everywhere).",
                    },
                },
                "required": ["scope", "description"],
            },
        ),
        Tool(
            name="check_locks",
            description="Check if files are locked by other team members before editing. Returns lock status and who owns locks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to check (e.g., ['src/main.py', 'backend/auth.py'])",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Absolute git root the paths belong to. When given, locks "
                                       "anchored to a DIFFERENT repo are ignored (their patterns "
                                       "are relative to that repo). Omit = consider all locks.",
                    },
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="whos_editing",
            description="Before editing, check who else is ACTIVELY editing these files right now (live presence + their intent). Use to self-coordinate: if someone is already in a file, pick another, wait, or coordinate. Distinct from check_locks (declared scope locks) — this is real-time 'who has hands on it now'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths you're about to edit (e.g., ['src/auth/jwt.py'])",
                    },
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="request_override",
            description="Request permission to work on files locked by someone else. Use when blocked by exclusive lock. Keywords 'urgent', 'security', 'hotfix', 'critical' may auto-approve.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The lock pattern blocking you (e.g., 'backend/**')",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Why you need access. Use keywords: urgent, security, hotfix, critical for faster approval.",
                    },
                },
                "required": ["pattern", "justification"],
            },
        ),
        Tool(
            name="check_pending_requests",
            description="Check if anyone is requesting permission to override your locks. Call this periodically to respond to requests.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="respond_to_request",
            description="Approve or deny an override request from another team member.",
            inputSchema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "The override request ID to respond to",
                    },
                    "approved": {
                        "type": "boolean",
                        "description": "True to approve, False to deny",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message to send with your response",
                    },
                },
                "required": ["request_id", "approved", "message"],
            },
        ),
        Tool(
            name="record_restart",
            description=(
                "Record that you restarted a SHARED service (a build server, a model "
                "runner, an API other agents depend on). Call this right after "
                "`systemctl restart`. A restart drops queued prompts, kills in-flight "
                "renders and deploys whatever is on disk, and it is invisible to every "
                "other session unless it is recorded here — team_status shows only a "
                "decision COUNT, never the content. Capture before/after numbers when "
                "you have them so 'did it help' is answerable later."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": "systemd unit, e.g. 'my-api' ('.service' and case are normalized)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "WHY it was bounced, and who authorized it",
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["completed", "failed", "in_progress"],
                        "description": "'completed' by default; 'failed' if it did not come back",
                        "default": "completed",
                    },
                    "old_pid": {"type": "integer", "description": "PID before the restart"},
                    "new_pid": {"type": "integer", "description": "PID after the restart"},
                    "before": {
                        "type": "object",
                        "description": "Measurements taken BEFORE (e.g. {'queue_depth': 0, 'ram_avail_gb': 22.7})",
                    },
                    "after": {
                        "type": "object",
                        "description": "Measurements taken AFTER, if already known",
                    },
                },
                "required": ["unit", "reason"],
            },
        ),
        Tool(
            name="recent_restarts",
            description=(
                "When was a shared service last bounced, by whom, and did it help? "
                "Check this BEFORE debugging a vanished prompt, a dead render or "
                "surprising code behaviour — a peer session may have just restarted "
                "the thing you are standing on."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": "Optional: limit to one unit, e.g. 'my-api'",
                    },
                    "limit": {"type": "integer", "description": "Max rows (default 20)"},
                },
            },
        ),
        Tool(
            name="delegate",
            description=(
                "Hand a BOUNDED subproblem to another worker through ATS. You keep "
                "ownership of the parent task the whole time — this creates a child "
                "record, never a handoff, and you must reconcile what comes back "
                "before accepting it. mode is enforced, not advisory: READ_ONLY and "
                "VERIFY children cannot write files, commit, restart services, submit "
                "GPU work, or delegate onward. Requires acceptance criteria: without "
                "them you cannot judge the result and 'it worked' becomes the test."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "description": "The ONE question or change."},
                    "acceptance": {"type": "string",
                                   "description": "What a satisfactory answer must contain."},
                    "mode": {"type": "string", "enum": ["READ_ONLY", "IMPLEMENT", "VERIFY"],
                             "description": "Authority envelope for the child. Start READ_ONLY."},
                    "task": {"type": "string", "description": "Parent task id, e.g. '2654'."},
                    "repo": {"type": "string", "description": "Absolute repo root."},
                    "scope": {"type": "array", "items": {"type": "string"},
                              "description": "Globs the child may edit. IMPLEMENT only."},
                    "worker": {"type": "string", "description": "Delegated worker (default claude-code)."},
                    "lease_minutes": {"type": "integer", "description": "Cap on the child's run."},
                },
                "required": ["objective", "acceptance"],
            },
        ),
        Tool(
            name="reconcile_delegation",
            description=(
                "Close the loop on a child delegation you own: ACCEPT it only after "
                "you have independently checked its claims against the acceptance "
                "criteria, or REJECT it. You cannot complete your own session while a "
                "child is unreconciled — you still own the task. Accepting is your "
                "judgement, not the child's report of success."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "delegation_id": {"type": "string"},
                    "state": {"type": "string", "enum": ["closed", "rejected"],
                              "description": "'closed' = accepted, 'rejected' = not satisfied."},
                    "verdict": {"type": "string",
                                "description": "What YOU verified, and how. Not a restatement of the child's claim."},
                },
                "required": ["delegation_id", "state", "verdict"],
            },
        ),
        Tool(
            name="preflight",
            description=(
                "Before you spend real work — a render, a GPU canary, a migration, "
                "a bounded edit — ask whether this exact action has already been "
                "tried and what happened. Returns a disposition (CLEAR / CAUTION / "
                "STRONG_WARNING / INSUFFICIENT_EVIDENCE) with every citation behind "
                "it: failed approaches, operator rulings, prior sessions, failure "
                "clusters, and whether anything material changed since. ADVISORY: it "
                "changes nothing and does not block you. A STRONG_WARNING means "
                "somebody already bought that answer; read the citations before "
                "buying it again."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string",
                                  "description": "The action you are about to take."},
                    "operation_type": {"type": "string",
                                       "enum": ["investigate", "edit", "test", "render",
                                                "gpu_canary", "service_restart",
                                                "migration", "other"]},
                    "repo_root": {"type": "string"},
                    "scope": {"type": "array", "items": {"type": "string"},
                              "description": "Path globs the action would touch."},
                    "task_id": {"type": "integer", "description": "Tower task id, if any."},
                    "entities": {"type": "object",
                                 "description": ("Structured specifics where known: "
                                                 "scene, shot, cast, method, lane, "
                                                 "model, subsystem, error_signature.")},
                },
                "required": ["objective"],
            },
        ),
        Tool(
            name="task_brief",
            description=(
                "The context packet for a piece of work: live blockers, prior ATS "
                "decisions, prior work in this scope, and recall from a memory service "
                "if one is configured, each "
                "line carrying its provenance (OBSERVATION / INFERRED / VERIFIED / "
                "OPERATOR_DECISION) and a citation you can check. Ask BEFORE "
                "investigating or spending a canary — it is how you find out a lane "
                "is already known to fail. start_session returns one automatically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string",
                                  "description": "What you are about to do, in a sentence."},
                    "repo_root": {"type": "string",
                                  "description": "Absolute git root, to scope decisions to this repo."},
                    "scope": {"type": "array", "items": {"type": "string"},
                              "description": "Path globs you expect to touch."},
                    "recall": {"type": "boolean",
                               "description": "Consult the configured memory service (default true)."},
                },
                "required": ["objective"],
            },
        ),
        Tool(
            name="delegation_status",
            description=(
                "Read-only lifecycle view of one delegation: full delegation, parent "
                "and child session ids, mode, state, the child's EFFECTIVE authority, "
                "timestamps, and each session's status and lock count. Enough to "
                "verify a delegation without reading the database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "delegation_id": {"type": "string", "description": "Full delegation id."},
                },
                "required": ["delegation_id"],
            },
        ),
        Tool(
            name="ats_version",
            description=(
                "Build identity of the ATS surfaces you are talking to: the commit "
                "this MCP process is running, the commit the REST service is running, "
                "and whether they MATCH. Call this before a contract test. An MCP "
                "server is spawned once per client session and keeps its tool catalog "
                "for that whole session, so a deploy made after your session started "
                "is invisible to you — which reads as 'the feature does not exist' "
                "rather than 'your process is old'."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="my_authority",
            description=(
                "What THIS worker may do: its capabilities, its edit/commit/close "
                "authority, and its concurrency cap. Ask before claiming scope or "
                "offering to close work — authority is declared server-side and is "
                "the same answer for every client. Pass `worker` to look up another "
                "worker instead (e.g. to decide who to hand a job to)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "worker": {
                        "type": "string",
                        "description": ("Worker NAME to look up (base authority only). "
                                        "Omit for yourself."),
                    },
                    "session_id": {
                        "type": "string",
                        "description": ("Full session id. Answers with EFFECTIVE authority, "
                                        "i.e. base narrowed by any delegation mode."),
                    },
                },
            },
        ),
        Tool(
            name="team_status",
            description="See what team members are currently working on. Shows active sessions and their scope.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="complete_session",
            description=(
                "Complete a session and release its locks. PASS session_id: it is "
                "authoritative for the call, is verified against this process's own "
                "pointer rather than silently replaced by it, and is echoed back with "
                "the prior and resulting status so you can see exactly which row "
                "changed. Omitting it is the legacy path — the session is resolved "
                "through the guarded pointer, which still refuses a shared/ambiguous "
                "one. You may only complete a session you own; a parent does not close "
                "its child's record and a child does not close its parent's."),
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Summary of what you accomplished",
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "The exact session to complete. Authoritative for this "
                            "call. If this process's own pointer names a DIFFERENT "
                            "live session, the call is refused rather than guessing. "
                            "Omit only for the legacy pointer-resolved path."),
                    },
                },
                "required": ["summary"],
            },
        ),
        Tool(
            name="log_decision",
            description="Log a design decision made during your session. Helps team understand why choices were made.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Decision title (e.g., 'Use JWT for auth')",
                    },
                    "chosen": {
                        "type": "string",
                        "description": "What was chosen",
                    },
                    "rejected": {
                        "type": "string",
                        "description": "What was rejected (optional)",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Why this decision was made",
                    },
                },
                "required": ["title", "chosen", "reasoning"],
            },
        ),

        # NEW: Phase 1 tools (Critical)
        Tool(
            name="pause_session",
            description="Pause your current session while keeping locks. Use when switching tasks temporarily.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="resume_session",
            description="Resume a paused session. Reactivates your locks and session.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_session_details",
            description="Get detailed information about your current session including locks, decisions, and commits.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="check_my_override_requests",
            description="Check status of override requests you've made (sent TO others).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),

        # NEW: Phase 2 tools (High value)
        Tool(
            name="check_git_changes",
            description="Check uncommitted files in your session scope. Helps verify what will be committed.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_all_locks",
            description="List all active locks across the team. Shows overall coordination landscape.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),

        # NEW: Phase 3 tools (Nice to have)
        Tool(
            name="get_decision_history",
            description="Get all decisions logged during your current session.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="pre_commit_check",
            description="Check if staged files have lock conflicts before committing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths to check (usually staged files)",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Absolute git root of the repo being committed. When "
                                       "given, locks anchored to a DIFFERENT repo are ignored. "
                                       "Omit = consider all locks.",
                    },
                },
                "required": ["paths"],
            },
        ),

        # NEW: Phase 4 tools (Polish)
        Tool(
            name="delete_lock",
            description="Remove a specific lock without completing the session. Use carefully.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lock_id": {
                        "type": "string",
                        "description": "The lock ID to delete",
                    },
                },
                "required": ["lock_id"],
            },
        ),
        Tool(
            name="extend_scope",
            description=(
                "Add file-glob patterns to your CURRENT session's scope mid-session, "
                "without starting a new session. Updates the board scope and creates "
                "enforceable locks for the new patterns. Use when your work grows into "
                "files outside the scope you declared at start_session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Path globs to add (e.g. ['ai-team-sync/src/**']). Globs, not prose.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["advisory", "exclusive"],
                        "description": "Lock mode for the new patterns (default advisory).",
                    },
                },
                "required": ["patterns"],
            },
        ),
        Tool(
            name="get_override_request_details",
            description="Get detailed information about a specific override request.",
            inputSchema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "The override request ID",
                    },
                },
                "required": ["request_id"],
            },
        ),
    ]


# Tools that already surface the override inbox themselves — appending the
# piggyback nudge to these would just duplicate their own output.
_OVERRIDE_NUDGE_SKIP = {"check_pending_requests", "respond_to_request",
                        "get_override_request_details"}


def format_override_nudge(requests: list, session_id: str) -> str | None:
    """One-line-per-request nudge when OTHER sessions are waiting on YOUR lock.

    Pure (unit-testable). Owner-side pending requests only; full ids so the
    holder can paste straight into respond_to_request. Returns None when quiet.
    """
    incoming = [
        r for r in (requests or [])
        if r.get("owner_session_id") == session_id
        and str(r.get("status", "")).lower() == "pending"
    ]
    if not incoming:
        return None
    lines = [f"⚠️ {len(incoming)} pending override request(s) awaiting YOUR response:"]
    for r in incoming:
        who = r.get("requester_developer") or "unknown"
        lines.append(
            f"  • {r.get('id')} — {who} wants '{r.get('conflicting_pattern', '?')}'"
        )
    lines.append("Respond with respond_to_request (approve/deny) — requests expire "
                 "after 15 minutes and the requester is stalled until you answer.")
    return "\n".join(lines)


def _fmt_age(seconds: float) -> str:
    """Compact age for display: '3m ago', '2h ago'."""
    seconds = max(0.0, float(seconds or 0))
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


#: How far back team_status looks for restarts. A bounce matters to a peer session
#: while its effects are still in play -- a vanished prompt, a reloaded deploy --
#: not forever, and an all-time list would bury today's under history.
RESTART_WINDOW_SECONDS = 6 * 3600


async def _recent_restarts_block(client: httpx.AsyncClient) -> str:
    """Render recent shared-service restarts for team_status (#2559).

    This is the half that makes recording worth anything. team_status prints only
    `Decisions: N` -- a count -- so a restart logged as a generic decision was
    invisible to every other session. "comfyui was bounced 3 minutes ago" is exactly
    what a session needs before it starts debugging why its prompt disappeared.

    Fails SILENT and empty: an older ats-server without /api/restarts must not break
    team_status, which is the tool sessions rely on to see each other at all.
    """
    try:
        resp = await client.get(f"{SERVER_URL}/api/restarts", params={"limit": 10})
        if resp.status_code != 200:
            return ""
        restarts = resp.json()
    except Exception:
        return ""

    recent = [r for r in restarts if (r.get("age_seconds") or 0) <= RESTART_WINDOW_SECONDS]
    if not recent:
        return ""

    out = "\n\U0001f501 Shared services restarted recently:\n"
    for r in recent:
        who = r.get("developer") or "unknown"
        line = f"• {r['unit']} — {_fmt_age(r.get('age_seconds'))} by {who}"
        if r.get("outcome") == "failed":
            line += "  \u274c FAILED"
        elif r.get("outcome") == "in_progress":
            line += "  \u23f3 IN PROGRESS"
        out += line + "\n"
        if r.get("reason"):
            out += f"  {r['reason'][:150]}\n"
    out += ("  (a restart drops queued prompts, kills in-flight renders, and deploys "
            "whatever is on disk)\n")
    return out


async def _incoming_override_nudge(client: httpx.AsyncClient, tool_name: str,
                                   session_id: str | None) -> str | None:
    """Piggyback layer (ats-override-push-p01): any ATS tool touch surfaces
    override requests pending ON this session, so a busy autonomous session
    hears about them without the operator relaying. Best-effort — never
    breaks the actual tool call."""
    if not session_id or tool_name in _OVERRIDE_NUDGE_SKIP:
        return None
    try:
        response = await client.get(
            f"{SERVER_URL}/api/override-requests",
            params={"session_id": session_id, "status": "pending"},
            timeout=2.0,
        )
        response.raise_for_status()
        requests = response.json()
    except Exception:
        return None
    return format_override_nudge(
        requests if isinstance(requests, list) else [], session_id)


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle MCP tool calls, then piggyback the override-request inbox."""
    result = await _call_tool_impl(name, arguments)
    try:
        session_id = load_session_id()
        if session_id:
            async with httpx.AsyncClient(timeout=2.0) as client:
                nudge = await _incoming_override_nudge(client, name, session_id)
            if nudge:
                result = list(result) + [TextContent(type="text", text=nudge)]
    except Exception:
        pass  # the nudge is advisory; the tool result must always go through
    return result


async def _delegation_child(client) -> str | None:
    """The child session this process is bound to, or None.

    A delegated child's authority over its OWN row is the delegation binding,
    not its label: ATS created that row for this delegation, which is exact
    where a name comparison is not (child_env writes the label, so comparing
    labels would compare the parent's own text with itself).
    """
    deleg_id = (os.environ.get("ATS_DELEGATION") or "").strip()
    if not deleg_id:
        return None
    try:
        r = await client.get(f"{SERVER_URL}/api/delegations/{deleg_id}")
        return r.json().get("child_session_id") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — no binding proven; caller falls back to label
        return None


async def _call_tool_impl(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle MCP tool calls."""
    # Identity AND where it came from. A read may use any of it; a mutation may
    # not use the shared pointer (see mutation_refusal).
    active_session_id, identity_source = resolve_identity()

    async def deny_mutation(client) -> str | None:
        """Refusal text for a session-mutating call, or None to proceed."""
        # A delegated child's authority is its DELEGATION BINDING, not its name.
        # If the delegation ATS created names this exact session as its child,
        # that is proof no label comparison can improve on.
        deleg_id = (os.environ.get("ATS_DELEGATION") or "").strip()
        if deleg_id and active_session_id:
            try:
                r = await client.get(f"{SERVER_URL}/api/delegations/{deleg_id}")
                if r.status_code == 200 and \
                        r.json().get("child_session_id") == active_session_id:
                    return None
            except Exception:
                pass  # fall through to the normal checks

        row = None
        if active_session_id and identity_source in ("env", "per_session"):
            try:
                r = await client.get(f"{SERVER_URL}/api/sessions/{active_session_id}")
                row = r.json() if r.status_code == 200 else None
            except Exception:
                row = None
        return mutation_refusal(active_session_id, identity_source, row,
                                session_agent_label())

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Liveness on ANY tool use (ats-sessionstart-orphan-adoption-p01): a
        # session that only ever talks through tools previously never
        # heartbeated, so live sessions looked orphaned on the board.
        # Fire-and-forget; a failed beat must never break the actual call.
        if active_session_id:
            try:
                await client.post(
                    f"{SERVER_URL}/api/sessions/{active_session_id}/heartbeat")
            except Exception:
                pass

        try:
            # Original tools
            if name == "start_session":
                scope = arguments["scope"]
                description = arguments["description"]
                exclusive = arguments.get("exclusive", False)

                response = await client.post(
                    f"{SERVER_URL}/api/sessions",
                    json={
                        "developer": get_git_user(),
                        "agent": session_agent_label(),
                        "scope": scope,
                        "description": description,
                        "branch": get_git_branch(),
                        "repo_root": arguments.get("repo_root", ""),
                        "auto_lock": True,
                        "lock_mode": "exclusive" if exclusive else "advisory",
                    },
                )

                if response.status_code == 409:
                    error = response.json()
                    conflicts = error["detail"].get("conflicts", [])
                    msg = f"❌ Cannot start session - conflicts detected:\n\n"
                    for c in conflicts:
                        msg += f"  • Pattern '{c['new_pattern']}' conflicts with '{c['existing_pattern']}'\n"
                        msg += f"    Held by: {c['existing_developer']} ({c['lock_mode']} lock)\n\n"
                    msg += format_conflict_guidance(conflicts)
                    return [TextContent(type="text", text=msg)]

                response.raise_for_status()
                data = response.json()
                save_session_id(data["id"])
                global _IN_PROCESS_SESSION_ID
                _IN_PROCESS_SESSION_ID = data["id"]

                # Adopt-or-complete the SessionStart auto-registration
                # (ats-sessionstart-orphan-adoption-p01): the hook registers a
                # placeholder session per agent; without this, start_session
                # creates a sibling and the placeholder lingers 'active'
                # forever (orphan class observed 2x on 2026-07-02).
                adopted = 0
                try:
                    sess_resp = await client.get(f"{SERVER_URL}/api/sessions")
                    sess_resp.raise_for_status()
                    my_label = session_agent_label()
                    for s in sess_resp.json():
                        # Prefix match via the SSOT predicate, NOT string equality.
                        # The old `== "auto-registered on SessionStart"` could never
                        # be true — the hook writes a guidance tail after that
                        # prefix — so adoption never fired and every agent
                        # accumulated a permanent ghost row alongside its real
                        # session (observed 2026-08-24: agent claude-code:7bb1e161
                        # active as both cf21607b and 4215bbc3).
                        if (s.get("status") == "active"
                                and s.get("id") != data["id"]
                                and s.get("agent") == my_label
                                and is_autoregistered(s.get("description"))
                                and not s.get("lock_count")):
                            await client.patch(
                                f"{SERVER_URL}/api/sessions/{s['id']}",
                                json={"status": "completed",
                                      "summary": adopted_summary(data["id"])})
                            adopted += 1
                except Exception:
                    pass  # adoption is best-effort; never block session start

                msg = f"✅ Session started!\n\n"
                if adopted:
                    msg += f"(auto-registered placeholder session completed: {adopted})\n"
                msg += f"Session ID: {data['id']}\n"
                msg += f"Scope: {', '.join(data['scope'])}\n"
                msg += f"Branch: {data['branch']}\n"
                msg += f"Locks created: {data['lock_count']}\n"
                msg += f"Mode: {'EXCLUSIVE (blocks all overlaps)' if exclusive else 'Advisory (warns on overlaps)'}\n\n"
                msg += "Team has been notified. Use complete_session when done."

                # The claim IS the trigger for context (brief-on-claim). A worker
                # that starts with prior decisions, live blockers and recall is a
                # worker that does not re-derive them or re-run a lane already
                # known to fail. Best-effort: a missing brief never fails a claim.
                try:
                    brief_resp = await client.post(
                        f"{SERVER_URL}/api/brief",
                        json={"objective": description,
                              "repo_root": arguments.get("repo_root", ""),
                              "scope": scope, "limit": 6},
                        timeout=25,
                    )
                    brief_resp.raise_for_status()
                    rendered = (brief_resp.json() or {}).get("rendered", "")
                    if rendered:
                        msg += "\n\n" + "-" * 60 + "\n" + rendered
                except Exception as exc:  # noqa: BLE001
                    msg += f"\n\n(no task brief: {type(exc).__name__})"

                return [TextContent(type="text", text=msg)]

            elif name == "check_locks":
                paths = arguments["paths"]
                response = await client.post(
                    f"{SERVER_URL}/api/locks/check",
                    json={"paths": paths,
                          "repo_root": arguments.get("repo_root", "")},
                )
                response.raise_for_status()
                results = response.json()

                locked = [r for r in results if r["locked"]]
                if not locked:
                    return [TextContent(type="text", text="✅ No locks found. All files are available.")]

                msg = "🔒 Lock conflicts detected:\n\n"
                for r in locked:
                    icon = "⛔" if r["mode"] == "exclusive" else "⚠️"
                    msg += f"{icon} {r['path']}\n"
                    msg += f"   Locked by: {r['developer']}\n"
                    msg += f"   Pattern: {r['pattern']}\n"
                    msg += f"   Mode: {r['mode']}\n"
                    # Lock id makes a stale conflict reapable via delete_lock(lock_id).
                    msg += f"   Lock ID: {r.get('lock_id', '?')}  (reap: delete_lock)\n\n"

                exclusive = [r for r in locked if r["mode"] == "exclusive"]
                if exclusive:
                    msg += "⛔ Exclusive locks block you. Use request_override to ask permission."
                else:
                    msg += "⚠️ Advisory locks - you can proceed but coordinate with team members."

                return [TextContent(type="text", text=msg)]

            elif name == "whos_editing":
                paths = arguments["paths"]
                response = await client.post(
                    f"{SERVER_URL}/api/presence/check",
                    json={"paths": paths, "exclude_developer": get_git_user(),
                          "exclude_agent": session_agent_label()},
                )
                response.raise_for_status()
                results = response.json()

                # ALSO consult declared scope locks before claiming clearance.
                # /presence/check answers "who has hands on it in the last few
                # minutes" and NOTHING else -- its own docstring says "Live
                # presence only -- declared scope locks are a separate check".
                # Saying "Clear to go" off that alone is a false all-clear, and
                # it fired for real: on 2026-08-25 this returned clear for
                # scene_generation_ops_routes_regenerate.py while session
                # 0fd3a304 held an advisory lock on that exact path, and the
                # caller shipped the file's uncommitted contents in a restart.
                # A second session got the same clear for clip_depth_transfer.py
                # against two live locks. The presence store was [] both times.
                #
                # Presence is the weaker signal and can be empty for reasons the
                # caller cannot see (the hook not firing, a session that never
                # opened the file). Locks are DECLARED and durable. A tool whose
                # answer gates an edit has to consult the durable one.
                locked_rows: list[dict] = []
                try:
                    lock_resp = await client.post(
                        f"{SERVER_URL}/api/locks/check", json={"paths": paths})
                    lock_resp.raise_for_status()
                    locked_rows = [r for r in lock_resp.json() if r.get("locked")]
                except Exception as _lk_err:  # noqa: BLE001
                    # Never let a lock-check failure turn into a silent clear:
                    # degrade to a NAMED unknown, not to "Clear to go".
                    return [TextContent(type="text", text=(
                        "⚠️ Live presence checked, but the scope-lock check "
                        f"failed ({_lk_err}). Cannot confirm this path is free "
                        "— run check_locks before editing."))]

                busy = [r for r in results if r["editors"]]
                if not busy and not locked_rows:
                    return [TextContent(
                        type="text",
                        text="✅ Nobody else is editing these files right now, "
                             "and no scope lock covers them. Clear to go.",
                    )]

                if locked_rows:
                    msg = "🔒 Declared scope lock(s) cover these paths:\n\n"
                    for r in locked_rows:
                        who = r.get("developer") or "unknown"
                        sess = str(r.get("session_id") or "")
                        msg += (f"   {r['path']}\n      {r.get('mode','advisory')}"
                                f" lock held by {who} (session {sess})"
                                f" via pattern {r.get('pattern')}\n")
                    if not busy:
                        msg += ("\nNobody has LIVE presence on them — a lock with "
                                "no presence still means an owner. Ask before "
                                "editing; do not read the quiet as free.\n")
                        return [TextContent(type="text", text=msg)]
                    msg += "\n"
                else:
                    msg = ""

                msg += "👤 Someone else is actively editing right now:\n\n"
                for r in busy:
                    msg += f"📝 {r['path']}\n"
                    for e in r["editors"]:
                        intent = f" — {e['intent']}" if e.get("intent") else ""
                        msg += f"   {e['developer']} ({e['agent']}){intent}\n"
                    msg += "\n"
                msg += "Consider editing a different file, waiting, or coordinating before you touch these."
                return [TextContent(type="text", text=msg)]

            elif name == "request_override":
                refusal = await deny_mutation(client)
                if refusal:
                    return [TextContent(type="text", text=f"❌ {refusal}")]
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Start a session first with start_session.")]

                pattern = arguments["pattern"]
                justification = arguments["justification"]

                response = await client.post(
                    f"{SERVER_URL}/api/override-requests",
                    json={
                        "requester_session_id": active_session_id,
                        "conflicting_pattern": pattern,
                        "justification": justification,
                    },
                )
                response.raise_for_status()
                data = response.json()

                # Check for auto-approval
                status = data.get("status", "pending")
                auto_decided = status in ("approved", "denied") and data.get("responded_at")

                if auto_decided:
                    if status == "approved":
                        msg = f"✅ Override request AUTO-APPROVED!\n\n"
                        msg += f"🤖 Reason: {data.get('response_message', 'Auto-approved based on policy')}\n"
                    else:
                        msg = f"❌ Override request AUTO-DENIED!\n\n"
                        msg += f"🤖 Reason: {data.get('response_message', 'Auto-denied based on policy')}\n"
                else:
                    msg = f"⏳ Override request sent (pending approval)...\n\n"

                msg += f"Request ID: {data['id']}\n"
                msg += f"Pattern: {data['conflicting_pattern']}\n"
                msg += f"Owner: {data['owner_developer']}\n"

                if not auto_decided:
                    msg += f"Expires: {data['expires_at']}\n\n"
                    msg += "💡 Tip: Use keywords 'urgent', 'security', 'hotfix', 'critical' for auto-approval\n"
                    msg += "Use check_my_override_requests to monitor response.\n"
                    msg += ("If it EXPIRES unanswered and the lock is ADVISORY: proceed with a "
                            "tightly-scoped change + log_decision (visible to the holder); "
                            "escalate to the operator only as a last resort.")

                return [TextContent(type="text", text=msg)]

            elif name == "check_pending_requests":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Start a session first.")]

                response = await client.get(
                    f"{SERVER_URL}/api/override-requests",
                    params={"session_id": active_session_id, "status": "pending"},
                )
                response.raise_for_status()
                requests = response.json()

                # The API returns rows where this session is requester OR owner;
                # 'pending requests TO YOU' means owner-side only (the requester
                # view is check_my_override_requests).
                requests = [r for r in requests
                            if r.get("owner_session_id") == active_session_id]

                if not requests:
                    return [TextContent(type="text", text="✅ No pending override requests.")]

                msg = f"📬 {len(requests)} pending override request(s) TO YOU:\n\n"
                for req in requests:
                    msg += f"Request ID: {req['id']}\n"
                    msg += f"From: {req['requester_developer']}\n"
                    msg += f"Pattern: {req['conflicting_pattern']}\n"
                    msg += f"Justification: {req['justification']}\n"
                    msg += f"Expires: {req['expires_at']}\n\n"

                msg += "Use respond_to_request to approve or deny."
                return [TextContent(type="text", text=msg)]

            elif name == "respond_to_request":
                request_id = arguments["request_id"]
                approved = arguments["approved"]
                message = arguments["message"]

                response = await client.post(
                    f"{SERVER_URL}/api/override-requests/{request_id}/respond",
                    json={"approved": approved, "message": message},
                )
                response.raise_for_status()
                data = response.json()

                status = "✅ APPROVED" if approved else "❌ DENIED"
                msg = f"{status} Override request response sent!\n\n"
                msg += f"Request ID: {data['id']}\n"
                msg += f"Requester: {data['requester_developer']}\n"
                msg += f"Your message: {message}\n\n"
                msg += "Requester has been notified."

                return [TextContent(type="text", text=msg)]

            elif name == "record_restart":
                payload = {
                    "unit": arguments["unit"],
                    "reason": arguments.get("reason", ""),
                    "outcome": arguments.get("outcome", "completed"),
                    "before": arguments.get("before") or {},
                    "after": arguments.get("after") or {},
                }
                for key in ("old_pid", "new_pid"):
                    if arguments.get(key) is not None:
                        payload[key] = arguments[key]
                # Attribute to this session when there is one, but a restart with no
                # session is still worth recording -- an out-of-band bounce is exactly
                # the kind that leaves everyone else confused.
                if active_session_id:
                    payload["session_id"] = active_session_id
                else:
                    payload["developer"] = get_git_user()

                response = await client.post(f"{SERVER_URL}/api/restarts", json=payload)
                if response.status_code == 404 and active_session_id:
                    # Stale pointer (reaped session) must not silently lose the record.
                    payload.pop("session_id", None)
                    payload["developer"] = get_git_user()
                    response = await client.post(f"{SERVER_URL}/api/restarts", json=payload)
                response.raise_for_status()
                rec = response.json()

                msg = f"\U0001f501 Recorded restart of {rec['unit']}\n\n"
                msg += f"Outcome: {rec['outcome']}\n"
                if rec.get("old_pid") or rec.get("new_pid"):
                    msg += f"PID: {rec.get('old_pid') or '?'} -> {rec.get('new_pid') or '?'}\n"
                if rec.get("reason"):
                    msg += f"Reason: {rec['reason']}\n"
                msg += "\nVisible to every other session via team_status."
                if not rec.get("after"):
                    msg += ("\nMeasure the effect once it settles and PATCH "
                            f"/api/restarts/{rec['id']} with `after` to answer 'did it help'.")
                return [TextContent(type="text", text=msg)]

            elif name == "recent_restarts":
                params: dict[str, Any] = {"limit": arguments.get("limit", 20)}
                if arguments.get("unit"):
                    params["unit"] = arguments["unit"]
                response = await client.get(f"{SERVER_URL}/api/restarts", params=params)
                response.raise_for_status()
                rows = response.json()

                if not rows:
                    scope = f" of {arguments['unit']}" if arguments.get("unit") else ""
                    return [TextContent(type="text", text=(
                        f"No recorded restarts{scope}.\n\n"
                        "NOTE: this means none were RECORDED, which is not the same as "
                        "none having happened -- restarts done outside ATS leave no trace."
                    ))]

                msg = f"\U0001f501 {len(rows)} recorded restart(s), newest first:\n\n"
                for r in rows:
                    msg += f"• {r['unit']} — {_fmt_age(r.get('age_seconds'))}"
                    msg += f" by {r.get('developer') or 'unknown'}"
                    if r.get("outcome") != "completed":
                        msg += f"  [{r['outcome']}]"
                    msg += "\n"
                    if r.get("old_pid") or r.get("new_pid"):
                        msg += f"  PID {r.get('old_pid') or '?'} -> {r.get('new_pid') or '?'}\n"
                    if r.get("reason"):
                        msg += f"  {r['reason'][:200]}\n"
                    if r.get("before") or r.get("after"):
                        msg += f"  before={r.get('before')} after={r.get('after')}\n"
                    msg += "\n"
                return [TextContent(type="text", text=msg)]

            elif name == "reconcile_delegation":
                response = await client.post(
                    f"{SERVER_URL}/api/delegations/{arguments['delegation_id']}/close",
                    json={"state": arguments["state"], "verdict": arguments["verdict"],
                          "actor_session_id": active_session_id or ""})
                if response.status_code >= 400:
                    return [TextContent(type="text", text=f"Refused: {response.text}")]
                d = response.json()
                return [TextContent(type="text", text=(
                    f"Delegation {d['id']} {d['state'].upper()}.\n"
                    f"Your verdict: {d['verdict']}\n"
                    f"Parent owner unchanged: {d['parent_owner_session_id']} (you)."))]

            elif name == "delegate":
                import subprocess as _sp
                import sys as _sys
                from pathlib import Path as _Path

                ats_bin = str(_Path(_sys.executable).parent / "ats")
                argv = [ats_bin, "delegate",
                        "--objective", arguments["objective"],
                        "--acceptance", arguments["acceptance"],
                        "--mode", arguments.get("mode", "READ_ONLY"),
                        "--worker", arguments.get("worker", "claude-code"),
                        "--lease-minutes", str(arguments.get("lease_minutes", 30))]
                if arguments.get("task"):
                    argv += ["--task", str(arguments["task"])]
                if arguments.get("repo"):
                    argv += ["--repo", arguments["repo"]]
                for pat in arguments.get("scope", []) or []:
                    argv += ["--scope", pat]

                lease = int(arguments.get("lease_minutes", 30))
                try:
                    proc = _sp.run(argv, capture_output=True, text=True,
                                   timeout=lease * 60 + 120)
                except _sp.TimeoutExpired:
                    return [TextContent(type="text",
                                        text="Delegation exceeded its lease and was abandoned. "
                                             "The child record stays open for you to reconcile.")]
                if proc.returncode != 0:
                    return [TextContent(type="text",
                                        text=f"Delegation refused:\n{proc.stderr.strip()}")]
                return [TextContent(
                    type="text",
                    text=(proc.stdout.strip() +
                          "\n\nYou still own the parent task. Verify the claims above "
                          "against the acceptance criteria before accepting them."))]

            elif name == "preflight":
                # Thin wrapper. Echo Brain owns the analysis so authority
                # ordering and evidence live in one place; ATS never decides a
                # disposition of its own.
                echo = os.environ.get("ECHO_BRAIN_URL", "http://localhost:8309")
                payload = {
                    "objective": arguments["objective"],
                    "requesting_worker": session_agent_label(),
                    "requesting_session": active_session_id or None,
                    "repo_root": arguments.get("repo_root", ""),
                    "tower_task_id": arguments.get("task_id"),
                    "operation_type": arguments.get("operation_type", "other"),
                    "declared_scope": arguments.get("scope", []),
                    "entities": arguments.get("entities", {}),
                }
                try:
                    r = await client.post(f"{echo}/api/preflight", json=payload, timeout=90)
                    r.raise_for_status()
                    d = r.json()
                except Exception as exc:  # noqa: BLE001
                    # Fail HONESTLY. An unavailable support service is unknown,
                    # never CLEAR — answering "go ahead" because the thing that
                    # would have warned you is down is the worst possible default.
                    return [TextContent(type="text", text=(
                        f"⚠ PREFLIGHT UNAVAILABLE ({type(exc).__name__}). This is NOT "
                        f"a CLEAR result: no history was checked. Echo Brain at {echo} "
                        f"did not answer. Proceed on your own judgement and operator "
                        f"rules, or retry."))]

                icon = {"CLEAR": "✅", "CAUTION": "⚠", "STRONG_WARNING": "⛔",
                        "INSUFFICIENT_EVIDENCE": "❔",
                        "ALREADY_COMPLETED": "🏁"}.get(d["disposition"], "•")
                lines = [f"{icon} {d['disposition']} — preflight {d['preflight_request_id']}",
                         "", f"  {d['recommended_next']}", ""]

                # LIVE TASK STATE, before any history. A closed task or one held
                # by another worker changes what the caller should do next more
                # than any amount of recall does, so it is not buried under the
                # evidence counts.
                ts = d.get("task_state") or None
                if ts:
                    lines += ["", f"  TOWER TASK #{ts.get('task_id')} "
                                  f"{ts.get('task_key') or ''}".rstrip(),
                              f"    status: {ts.get('status')}   gate: {ts.get('gate')}"
                              f"   priority: {ts.get('priority')}"]
                    if ts.get("is_closed"):
                        # The full verified_by is already in the recommendation
                        # above; repeating a 1KB JSON blob two lines later buries
                        # the rest of the block. Name the commit, point at it.
                        vb = ts.get("verified_by") or {}
                        commit = (vb.get("commit") if isinstance(vb, dict) else None)
                        lines.append(
                            f"    CLOSED — verified_by commit "
                            f"{str(commit)[:12] if commit else '(none recorded)'}"
                            f" (full evidence in the recommendation above)")
                    claim = ts.get("claim") or None
                    if claim and ts.get("claim_is_live"):
                        lines.append(f"    HELD BY: run {claim.get('run_id')} "
                                     f"({claim.get('state')}) {claim.get('executor')}, "
                                     f"lease until {claim.get('lease_expires_at')}")
                    if ts.get("recommendation"):
                        lines.append(f"    ruling: {ts['recommendation'][:200]}")
                    if ts.get("conflict_with_history"):
                        # Marker only. The full historical reading is already
                        # carried verbatim in the recommendation.
                        lines.append("    ⚠ live task state overrode the historical "
                                     "reading (see HISTORY ALSO SAYS above)")

                counts = d.get("evidence_by_authority") or {}
                if counts:
                    lines.append("  evidence by authority: " + ", ".join(
                        f"{k}={v}" for k, v in sorted(counts.items())))
                for label, key in (("OPERATOR RULINGS", "operator_decisions"),
                                   ("FAILED APPROACHES", "failed_approaches"),
                                   ("CHANGED SINCE", "change_since_evidence")):
                    items = d.get(key) or []
                    if items:
                        lines += ["", f"  {label}"]
                        for e in items[:4]:
                            lines.append(f"    [{e['authority_class']}] {e['citation']}")
                            if e.get("evidence_text"):
                                lines.append(f"        {e['evidence_text'][:160]}")
                if d.get("degraded_reason"):
                    lines += ["", f"  degraded: {d['degraded_reason']}"]
                lines += ["", "  Advisory. Nothing was changed; the decision is yours."]
                return [TextContent(type="text", text="\n".join(lines))]

            elif name == "task_brief":
                response = await client.post(
                    f"{SERVER_URL}/api/brief",
                    json={
                        "objective": arguments["objective"],
                        "repo_root": arguments.get("repo_root", ""),
                        "scope": arguments.get("scope", []),
                        "recall": arguments.get("recall", True),
                    },
                    timeout=30,
                )
                response.raise_for_status()
                return [TextContent(type="text", text=response.json()["rendered"])]

            elif name == "delegation_status":
                did = arguments["delegation_id"]
                r = await client.get(f"{SERVER_URL}/api/delegations/{did}")
                if r.status_code != 200:
                    return [TextContent(type="text", text=f"❌ {r.text}")]
                d = r.json()

                async def _session(sid):
                    if not sid:
                        return None
                    sr = await client.get(f"{SERVER_URL}/api/sessions/{sid}")
                    return sr.json() if sr.status_code == 200 else None

                parent = await _session(d["parent_owner_session_id"])
                child = await _session(d.get("child_session_id"))
                eff = None
                if d.get("child_session_id"):
                    ar = await client.get(
                        f"{SERVER_URL}/api/authority/{d['child_session_id']}")
                    eff = ar.json() if ar.status_code == 200 else None

                def _line(label, sess):
                    if not sess:
                        return f"  {label}: (not found)"
                    return (f"  {label}: {sess['id']}\n"
                            f"      agent {sess['agent']}  status {sess['status']}  "
                            f"locks {sess.get('lock_count', '?')}")

                lines = [f"🔗 delegation {d['id']}",
                         f"   mode {d['mode']}   state {d['state']}",
                         f"   created {d['created_at']}   closed {d['closed_at'] or '-'}",
                         f"   lease expires {d['lease_expires_at']}", "",
                         _line("parent OWNER", parent),
                         _line("child       ", child)]
                if eff:
                    e, b = eff["effective_authority"], eff["base_authority"]
                    lines += ["", f"  child base authority     : edit={b['edit']} "
                                  f"commit={b['commit']} close={b['task_close']}",
                              f"  child EFFECTIVE authority: edit={e['edit']} "
                              f"commit={e['commit']} close={e['task_close']}"]
                    if eff.get("prohibitions"):
                        lines.append("  child may not: " + ", ".join(eff["prohibitions"]))
                if d.get("result_summary"):
                    lines += ["", "  result: " + d["result_summary"][:400]]
                if d.get("verdict"):
                    lines += ["", "  owner verdict: " + d["verdict"][:400]]
                lines += ["", "  Ownership never moved: the parent above owns the task."]
                return [TextContent(type="text", text="\n".join(lines))]

            elif name == "ats_version":
                from ai_team_sync.build_info import identity, summary_line
                mine = identity("ats-mcp")
                rest: dict | None = None
                try:
                    r = await client.get(f"{SERVER_URL}/api/version", timeout=5)
                    rest = r.json() if r.status_code == 200 else None
                except Exception:
                    rest = None

                lines = ["🏷  ATS build identity", "", "  " + summary_line(mine)]
                if rest:
                    lines.append("  " + summary_line(rest))
                    if rest.get("commit") != mine.get("commit"):
                        lines += ["", ("  ⚠ SKEW: this MCP process and the REST service are on "
                                       "DIFFERENT commits. Your tool catalog was fixed when this "
                                       "session started; restart your client session to pick up a "
                                       "newer deploy. Do not conclude a feature is missing.")]
                    else:
                        lines.append("\n  MCP and REST agree.")
                else:
                    lines.append("  ats-rest: unreachable (could not compare)")
                lines += ["", "  tools in this catalog: " + str(len(await list_tools()))]
                return [TextContent(type="text", text="\n".join(lines))]

            elif name == "my_authority":
                args = arguments or {}
                sid = (args.get("session_id") or "").strip()
                label = (args.get("worker") or "").strip()
                if not sid and not label:
                    sid = active_session_id or ""

                # A session id can be answered with EFFECTIVE authority, because
                # the server knows whether that session is a delegated child.
                # A bare worker name can only be answered with base authority.
                if sid:
                    resp = await client.get(f"{SERVER_URL}/api/authority/{sid}")
                    if resp.status_code == 200:
                        a = resp.json()
                        base, eff = a["base_authority"], a["effective_authority"]
                        d = a.get("delegation")
                        lines = [f"🪪 session {a['session_id']}",
                                 f"   agent {a['agent']}  →  worker '{a['worker']}'", ""]
                        if d:
                            lines += [f"  delegation : {d['delegation_id']}",
                                      f"  mode       : {d['mode']} ({d['state']})",
                                      f"  parent owner: {d['parent_owner_session_id']}", ""]
                        def _fmt(x):
                            return (f"edit={x['edit']} commit={x['commit']} "
                                    f"close={x['task_close']}")
                        lines += [f"  base authority     : {_fmt(base)}",
                                  f"  EFFECTIVE authority: {_fmt(eff)}"]
                        if a.get("narrowed"):
                            lines += ["", "  ⚠ NARROWED by the delegation mode. The effective "
                                          "row is what applies; base is what this worker could "
                                          "do outside this delegation."]
                        if a.get("prohibitions"):
                            lines += ["", "  you may not: " + ", ".join(a["prohibitions"])]
                        lines += ["", "  capabilities: " + ", ".join(a["capabilities"])]
                        return [TextContent(type="text", text="\n".join(lines))]

                label = label or detect_agent()
                response = await client.get(f"{SERVER_URL}/api/workers/{label}")
                response.raise_for_status()
                w = response.json()
                auth = w["authority"]
                edit = {"none": "NO — read-only", "claimed_scope": "yes, inside a claimed scope"}.get(
                    auth["edit"], auth["edit"])
                close = {"no": "NO", "yes": "yes",
                         "conditional": "only against satisfied acceptance evidence"}.get(
                    auth["task_close"], auth["task_close"])
                cap = w["concurrency"]
                lines = [
                    f"🪪 {label}  →  worker '{w['worker']}'  ({w['cost_class']})",
                    "",
                    f"  edit files : {edit}",
                    f"  commit     : {'yes' if auth['commit'] else 'NO'}",
                    f"  close work : {close}",
                    f"  concurrency: {cap if cap is not None else 'uncapped'}",
                    "",
                    "  capabilities: " + ", ".join(w["capabilities"]),
                ]
                if auth["edit"] == "none":
                    lines += ["", ("  A read-only worker registers UNSCOPED and attaches findings. "
                                   "Claiming scope is refused by the server, not by your client.")]
                return [TextContent(type="text", text="\n".join(lines))]

            elif name == "team_status":
                response = await client.get(
                    f"{SERVER_URL}/api/sessions",
                    params={"status": "active"},
                )
                response.raise_for_status()
                sessions = response.json()

                if not sessions:
                    return [TextContent(type="text", text="✅ No active sessions. Team is available.")]

                stale_n = sum(1 for s in sessions if s.get("is_stale"))
                header = f"👥 {len(sessions)} active session(s)"
                if stale_n:
                    header += f" — ⚠ {stale_n} look STALE (idle/ghost; their scope is NOT a live blocker)"
                msg = header + ":\n\n"
                for s in sessions:
                    scope = ", ".join(s["scope"]) if s["scope"] else "no scope"
                    idle = s.get("idle_seconds")
                    idle_txt = f"{int(idle // 60)}m" if isinstance(idle, (int, float)) else "?"
                    if s.get("is_stale"):
                        tag = (f"  ⚠ STALE (idle {idle_txt} — likely a ghost; treat its scope as "
                               f"non-blocking. Reap its locks via delete_lock(lock_id), or it "
                               f"auto-completes on the reaper's next sweep)")
                    else:
                        tag = f"  (active, idle {idle_txt})"
                    msg += f"• {s['developer']} ({s['agent']}){tag}\n"
                    msg += f"  Scope: {scope}\n"
                    msg += f"  Branch: {s['branch']}\n"
                    msg += f"  Description: {s['description']}\n"
                    msg += f"  Locks: {s['lock_count']}  Decisions: {s['decision_count']}\n"
                    unc = s.get("uncommitted_in_scope") or []
                    if unc:
                        shown = ", ".join(unc[:5]) + ("…" if len(unc) > 5 else "")
                        msg += f"  ✎ Uncommitted in scope ({len(unc)}): {shown}\n"
                    msg += "\n"

                msg += await _recent_restarts_block(client)
                return [TextContent(type="text", text=msg)]

            elif name == "complete_session":
                summary = arguments["summary"]
                explicit_id = (arguments.get("session_id") or "").strip()

                # WHICH row, decided before anything is mutated. An explicit id is
                # authoritative; the pointer is a compatibility path. A pointer that
                # speaks for THIS caller and disagrees is a refusal, not a tiebreak
                # — guessing which of two live sessions was meant is exactly the
                # 2026-09-11 wrong-session completion.
                pointer_id, pointer_source = resolve_identity()

                # ORDER MATTERS, and a live canary is what taught us so. The
                # pointer-conflict rule used to run first, so naming a
                # nonexistent id, or another worker's id, was refused with a
                # CONFLICT message whose remedy reads "clear/point the pointer at
                # <that id> and retry" — advice that is wrong for a missing
                # session and actively dangerous for someone else's, since
                # following it would enable the very completion we forbid.
                # Existence and ownership are the more fundamental facts, so they
                # are settled first and the conflict rule speaks only about a row
                # the caller could legitimately complete.
                if explicit_id:
                    try:
                        rr = await client.get(f"{SERVER_URL}/api/sessions/{explicit_id}")
                        row = rr.json() if rr.status_code == 200 else None
                    except Exception:  # noqa: BLE001
                        row = None

                    deleg_child = await _delegation_child(client)
                    refusal = ownership_refusal(explicit_id, row,
                                                session_agent_label(),
                                                delegation_child_id=deleg_child)
                    if refusal:
                        return [TextContent(type="text", text=f"❌ {refusal}")]

                    live = None
                    if pointer_id and pointer_id != explicit_id:
                        try:
                            pr = await client.get(
                                f"{SERVER_URL}/api/sessions/{pointer_id}")
                            live = (pr.status_code == 200
                                    and pr.json().get("status") == "active")
                        except Exception:  # noqa: BLE001 — unknown stays LIVE
                            live = None

                    target = resolve_completion_target(
                        explicit_id=explicit_id, pointer_id=pointer_id,
                        pointer_source=pointer_source,
                        pointer_names_live_session=live)
                    if not target.ok:
                        return [TextContent(type="text", text=f"❌ {target.refusal}")]
                else:
                    # Legacy path: the pointer IS the candidate, so its own
                    # safety rule (never the shared file) must settle first.
                    target = resolve_completion_target(
                        explicit_id=None, pointer_id=pointer_id,
                        pointer_source=pointer_source)
                    if not target.ok:
                        return [TextContent(type="text", text=f"❌ {target.refusal}")]
                    try:
                        rr = await client.get(
                            f"{SERVER_URL}/api/sessions/{target.session_id}")
                        row = rr.json() if rr.status_code == 200 else None
                    except Exception:  # noqa: BLE001
                        row = None
                    refusal = ownership_refusal(
                        target.session_id, row, session_agent_label(),
                        delegation_child_id=await _delegation_child(client))
                    if refusal:
                        return [TextContent(type="text", text=f"❌ {refusal}")]

                prior_status = (row or {}).get("status")
                if prior_status in ("completed", "cancelled"):
                    # Terminal already. Re-PATCHing would re-stamp completed_at and
                    # re-emit the completion event to the memory clerk, inventing a
                    # second closure of one session. Report, mutate nothing.
                    return [TextContent(type="text", text=(
                        f"ℹ️  Session {target.session_id} is already {prior_status} "
                        f"(completed_at {(row or {}).get('completed_at')}). Nothing "
                        f"was changed.\n\n  existing summary: "
                        f"{((row or {}).get('summary') or '')[:300]}"))]

                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{target.session_id}",
                    json={"status": "completed", "summary": summary},
                )
                if response.status_code >= 400:
                    return [TextContent(type="text", text=(
                        f"❌ Completion refused for {target.session_id}: "
                        f"HTTP {response.status_code} {response.text[:400]}"))]
                after = response.json()

                lines = [
                    "✅ Session completed",
                    "",
                    f"  session    : {after.get('id')}",
                    f"  agent      : {after.get('agent')}",
                    f"  status     : {prior_status} → {after.get('status')}",
                    f"  completed  : {after.get('completed_at')}",
                    f"  targeted by: {target.source}",
                ]
                if target.conflict:
                    lines += ["", f"  ⚠ {target.conflict}"]
                lines += ["", f"  Summary: {summary}", "",
                          "  All locks released. Team has been notified."]

                # Only drop the pointer when it was OUR session; clearing it after
                # completing some other row would strand this process's own id.
                if not explicit_id or explicit_id == pointer_id:
                    clear_session_id()
                return [TextContent(type="text", text="\n".join(lines))]

            elif name == "log_decision":
                refusal = await deny_mutation(client)
                if refusal:
                    return [TextContent(type="text", text=f"❌ {refusal}")]
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Start a session first.")]

                title = arguments["title"]
                chosen = arguments["chosen"]
                rejected = arguments.get("rejected", "")
                reasoning = arguments["reasoning"]

                response = await client.post(
                    f"{SERVER_URL}/api/decisions",
                    json={
                        "session_id": active_session_id,
                        "title": title,
                        "chosen": chosen,
                        "rejected": rejected,
                        "reasoning": reasoning,
                        "files": [],
                    },
                )
                response.raise_for_status()

                msg = f"✅ Decision logged!\n\n"
                msg += f"Title: {title}\n"
                msg += f"Chosen: {chosen}\n"
                if rejected:
                    msg += f"Rejected: {rejected}\n"
                msg += f"Reasoning: {reasoning}\n\n"
                msg += "Team can view this decision in session history."

                return [TextContent(type="text", text=msg)]

            # NEW TOOLS - Phase 1 (Critical)

            elif name == "pause_session":
                refusal = await deny_mutation(client)
                if refusal:
                    return [TextContent(type="text", text=f"❌ {refusal}")]
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session to pause.")]

                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{active_session_id}",
                    json={"status": "paused"},
                )
                response.raise_for_status()
                data = response.json()

                msg = f"⏸️ Session paused!\n\n"
                msg += f"Session ID: {data['id']}\n"
                msg += f"Locks: {data['lock_count']} (retained)\n\n"
                msg += "Use resume_session to continue work."

                return [TextContent(type="text", text=msg)]

            elif name == "resume_session":
                refusal = await deny_mutation(client)
                if refusal:
                    return [TextContent(type="text", text=f"❌ {refusal}")]
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No session to resume.")]

                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{active_session_id}",
                    json={"status": "active"},
                )
                response.raise_for_status()
                data = response.json()

                msg = f"▶️ Session resumed!\n\n"
                msg += f"Session ID: {data['id']}\n"
                msg += f"Scope: {', '.join(data['scope'])}\n"
                msg += f"Locks: {data['lock_count']}\n"

                return [TextContent(type="text", text=msg)]

            elif name == "get_session_details":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session.")]

                response = await client.get(
                    f"{SERVER_URL}/api/sessions/{active_session_id}",
                )
                response.raise_for_status()
                data = response.json()

                scope = ", ".join(data["scope"]) if data["scope"] else "no scope"

                msg = f"📊 Session Details\n\n"
                msg += f"ID: {data['id']}\n"
                msg += f"Developer: {data['developer']}\n"
                msg += f"Agent: {data['agent']}\n"
                msg += f"Status: {data['status']}\n"
                msg += f"Branch: {data['branch']}\n"
                msg += f"Scope: {scope}\n"
                msg += f"Description: {data['description']}\n\n"
                msg += f"📈 Activity:\n"
                msg += f"  Locks: {data['lock_count']}\n"
                msg += f"  Decisions: {data['decision_count']}\n"
                msg += f"  Commits: {data['commit_count']}\n\n"
                msg += f"Started: {data['started_at']}\n"

                if data.get("summary"):
                    msg += f"Summary: {data['summary']}\n"

                return [TextContent(type="text", text=msg)]

            elif name == "check_my_override_requests":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session.")]

                response = await client.get(
                    f"{SERVER_URL}/api/override-requests",
                    params={"session_id": active_session_id},
                )
                response.raise_for_status()
                requests = response.json()

                # Filter to only requests FROM this session (as requester)
                my_requests = [r for r in requests if r["requester_session_id"] == active_session_id]

                if not my_requests:
                    return [TextContent(type="text", text="✅ No override requests sent.")]

                msg = f"📤 {len(my_requests)} override request(s) you've sent:\n\n"
                for req in my_requests:
                    status_icon = {"pending": "⏳", "approved": "✅", "denied": "❌", "expired": "⌛"}
                    icon = status_icon.get(req["status"], "?")

                    msg += f"{icon} Request ID: {req['id']}\n"
                    msg += f"   To: {req['owner_developer']}\n"
                    msg += f"   Pattern: {req['conflicting_pattern']}\n"
                    msg += f"   Status: {req['status']}\n"

                    if req.get("response_message"):
                        msg += f"   Response: {req['response_message']}\n"

                    if req["status"] == "expired":
                        msg += ("   ⌛ Expired unanswered. If the lock is ADVISORY: proceed with a "
                                "tightly-scoped change + log_decision; or re-file request_override "
                                "if the holder is now active. Operator escalation = last resort.\n")

                    msg += "\n"

                return [TextContent(type="text", text=msg)]

            # NEW TOOLS - Phase 2 (High value)

            elif name == "check_git_changes":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session.")]

                response = await client.get(
                    f"{SERVER_URL}/api/git/session/{active_session_id}/changes",
                )
                response.raise_for_status()
                data = response.json()

                # SessionChangesResponse returns uncommitted_files/
                # files_by_pattern/total_files. This read files_in_scope and
                # files_out_of_scope, which the endpoint has never sent, so
                # both were always [] and the tool always said "no changes" --
                # the same wiring mismatch already fixed once for
                # pre_commit_check (tests/test_mcp_pre_commit_check.py).
                # The endpoint filters BY scope, so there is no out-of-scope
                # list to render; the branch that claimed to show one is gone
                # rather than left reading a key nobody sends.
                in_scope = data.get("uncommitted_files", [])

                if not in_scope:
                    return [TextContent(type="text", text="✅ No uncommitted changes.")]

                msg = f"📝 {len(in_scope)} uncommitted file(s) in your scope:\n\n"
                for path in in_scope[:20]:  # Limit display
                    msg += f"  {path}\n"

                if len(in_scope) > 20:
                    msg += f"  ... and {len(in_scope) - 20} more\n"
                msg += "\n💡 Commit or stash before your session is reaped.\n"

                return [TextContent(type="text", text=msg)]

            elif name == "list_all_locks":
                response = await client.get(f"{SERVER_URL}/api/locks")
                response.raise_for_status()
                locks = response.json()

                if not locks:
                    return [TextContent(type="text", text="✅ No active locks.")]

                msg = f"🔒 {len(locks)} active lock(s):\n\n"
                for lock in locks:
                    mode_icon = "⛔" if lock["mode"] == "exclusive" else "⚠️"
                    msg += f"{mode_icon} {lock['pattern']} ({lock['mode']})\n"
                    msg += f"   Developer: {lock.get('developer', 'unknown')}\n"
                    # Surface the lock id so a stale/ghost lock is reapable via
                    # delete_lock(lock_id) without completing its (dead) session.
                    msg += f"   Lock ID: {lock['id']}  (reap: delete_lock)\n"
                    msg += f"   Expires: {lock['expires_at']}\n\n"

                return [TextContent(type="text", text=msg)]

            # NEW TOOLS - Phase 3 (Nice to have)

            elif name == "get_decision_history":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session.")]

                response = await client.get(
                    f"{SERVER_URL}/api/decisions",
                    params={"session_id": active_session_id},
                )
                response.raise_for_status()
                decisions = response.json()

                if not decisions:
                    return [TextContent(type="text", text="✅ No decisions logged yet.")]

                msg = f"📚 {len(decisions)} decision(s) in this session:\n\n"
                for d in decisions:
                    msg += f"**{d['title']}**\n"
                    msg += f"  Chose: {d['chosen']}\n"
                    if d.get("rejected"):
                        msg += f"  Rejected: {d['rejected']}\n"
                    msg += f"  Reasoning: {d['reasoning']}\n"
                    msg += f"  Logged: {d['created_at']}\n\n"

                return [TextContent(type="text", text=msg)]

            elif name == "pre_commit_check":
                paths = arguments["paths"]

                # NOTE: the endpoint field is `staged_files` (NOT `paths`) and it
                # returns `blocking_locks`/`advisory_locks` with a `file` key. The old
                # code sent `paths` and read `blocked`/`warned`, so the argument was
                # silently dropped (server auto-detected staged files from ITS OWN cwd)
                # and the response never parsed — the tool always said "clear". Fixed.
                response = await client.post(
                    f"{SERVER_URL}/api/git/pre-commit-check",
                    json={"staged_files": paths,
                          "repo_root": arguments.get("repo_root", "")},
                )
                response.raise_for_status()
                data = response.json()

                blocked = data.get("blocking_locks", [])
                warned = data.get("advisory_locks", [])

                if not blocked and not warned:
                    return [TextContent(type="text", text="✅ All files clear for commit.")]

                msg = ""

                if blocked:
                    msg += f"⛔ {len(blocked)} file(s) BLOCKED by exclusive locks:\n\n"
                    for f in blocked:
                        msg += f"  {f['file']}\n"
                        msg += f"    Locked by: {f['developer']} (pattern: {f['pattern']})\n"
                    msg += "\n❌ Commit will be blocked. Resolve conflicts first.\n\n"

                if warned:
                    msg += f"⚠️ {len(warned)} file(s) have advisory locks:\n\n"
                    for f in warned:
                        msg += f"  {f['file']}\n"
                        msg += f"    Locked by: {f['developer']} (pattern: {f['pattern']})\n"
                    msg += "\n💡 Commit allowed but coordinate with team.\n"

                return [TextContent(type="text", text=msg)]

            # NEW TOOLS - Phase 4 (Polish)

            elif name == "delete_lock":
                lock_id = arguments["lock_id"]

                response = await client.delete(f"{SERVER_URL}/api/locks/{lock_id}")
                response.raise_for_status()

                msg = f"🗑️ Lock deleted!\n\n"
                msg += f"Lock ID: {lock_id}\n\n"
                msg += "⚠️ Other team members have been notified."

                return [TextContent(type="text", text=msg)]

            elif name == "extend_scope":
                refusal = await deny_mutation(client)
                if refusal:
                    return [TextContent(type="text", text=f"❌ {refusal}")]
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Run start_session first.")]

                patterns = arguments["patterns"]
                mode = arguments.get("mode", "advisory")

                # Locks FIRST, scope second (#2756): the board must never declare
                # a pattern this session was refused. The old order patched the
                # merged scope, then announced "Scope extended" over refused locks.
                created, refused = [], []
                for pat in patterns:
                    lr = await client.post(
                        f"{SERVER_URL}/api/locks",
                        json={"session_id": active_session_id, "pattern": pat,
                              "reason": "extend_scope", "mode": mode},
                    )
                    if lr.status_code in (200, 201):
                        created.append(pat)
                        continue
                    # Any refusal is reported and the loop continues, so a lock
                    # already taken for an earlier pattern is never hidden by a
                    # later pattern's error.
                    try:
                        body = lr.json()
                        detail = body.get("detail") if isinstance(body, dict) else body
                    except ValueError:
                        detail = lr.text
                    why = str((detail.get("message") if isinstance(detail, dict) else detail)
                              or "refused")[:300]
                    refused.append((pat, lr.status_code,
                                    why if lr.status_code == 409 else f"HTTP {lr.status_code}: {why}"))

                # Merge only what was locked into the declared scope, de-duped.
                sess = await client.get(f"{SERVER_URL}/api/sessions/{active_session_id}")
                sess.raise_for_status()
                sess_data = sess.json()
                current = sess_data.get("scope") or []
                merged = list(dict.fromkeys([*current, *created]))
                if created:
                    patch_body: dict[str, Any] = {"scope": merged}
                    # Adoption variant (ats-sessionstart-orphan-adoption-p01): a
                    # session taking real locks must not keep the placeholder
                    # description — a working session with 'auto-registered on
                    # SessionStart' reads as an orphan on the board (observed
                    # 2026-07-02, agent 4f5c927a). Derive a minimal honest one.
                    # Same dead-equality bug as the adoption path above: this compared
                    # against a bare prefix the hook never writes, so a session that
                    # took real locks kept reading as an unclaimed orphan forever.
                    if is_autoregistered(sess_data.get("description")):
                        patch_body["description"] = derived_working_description(merged)
                    patch = await client.patch(
                        f"{SERVER_URL}/api/sessions/{active_session_id}",
                        json=patch_body,
                    )
                    patch.raise_for_status()

                if not refused:
                    msg = f"✅ Scope extended (+{len(created)} {mode} lock(s)).\n\n"
                elif created:
                    msg = (f"⚠️ Scope PARTIALLY extended: +{len(created)} {mode} lock(s), "
                           f"{len(refused)} refused.\n\n")
                else:
                    msg = "❌ Scope NOT extended: every requested lock was refused.\n\n"
                msg += "Now covering:\n" + ("\n".join(f"  • {p}" for p in merged) or "  (nothing)") + "\n"
                if refused:
                    msg += "\nRefused — not locked and not added to your scope:\n"
                    msg += "\n".join(f"  • {p}: {why}" for p, _code, why in refused) + "\n"
                    if any(code == 409 for _p, code, _why in refused):
                        msg += "Coordinate with the holder or request_override."
                return [TextContent(type="text", text=msg)]

            elif name == "get_override_request_details":
                request_id = arguments["request_id"]

                response = await client.get(
                    f"{SERVER_URL}/api/override-requests/{request_id}",
                )
                response.raise_for_status()
                data = response.json()

                status_icon = {"pending": "⏳", "approved": "✅", "denied": "❌", "expired": "⌛"}
                icon = status_icon.get(data["status"], "?")

                msg = f"{icon} Override Request Details\n\n"
                msg += f"ID: {data['id']}\n"
                msg += f"From: {data['requester_developer']}\n"
                msg += f"To: {data['owner_developer']}\n"
                msg += f"Pattern: {data['conflicting_pattern']}\n"
                msg += f"Status: {data['status']}\n\n"
                msg += f"Justification:\n{data['justification']}\n\n"
                msg += f"Created: {data['created_at']}\n"

                if data.get("responded_at"):
                    msg += f"Responded: {data['responded_at']}\n"
                    msg += f"Response: {data.get('response_message', 'No message')}\n"
                else:
                    msg += f"Expires: {data['expires_at']}\n"

                return [TextContent(type="text", text=msg)]

            else:
                return [TextContent(type="text", text=f"❌ Unknown tool: {name}")]

        except httpx.HTTPStatusError as e:
            error_text = f"❌ HTTP Error: {e.response.status_code}\n{e.response.text}"

            # Add helpful guidance for common errors
            if e.response.status_code == 404:
                error_text += "\n\n💡 Resource not found. Check IDs or session status."
            elif e.response.status_code == 409:
                error_text += "\n\n💡 Conflict detected. Use team_status to see active sessions."
            elif e.response.status_code == 410:
                error_text += "\n\n💡 Resource expired. Request has timed out."

            return [TextContent(type="text", text=error_text)]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ Error: {str(e)}")]


def create_mcp_server() -> Server:
    """Create and return the MCP server instance."""
    return mcp_server


async def main():
    """Run the MCP server."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            mcp_server.create_initialization_options(),
        )


def run() -> None:
    """Sync console-script entry point — the `ats-mcp` script targets this.

    The entry point must be sync: pointing it at the async `main` makes the wrapper
    call it and discard the coroutine ('coroutine was never awaited', exit 1), so the
    stdio server never starts.
    """
    asyncio.run(main())


if __name__ == "__main__":
    run()
