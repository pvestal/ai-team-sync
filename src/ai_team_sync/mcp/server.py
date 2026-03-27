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


# MCP Server instance
mcp_server = Server("ai-team-sync")

# Server URL from environment
SERVER_URL = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")

# Session file for persistence
SESSION_FILE = Path.home() / ".ats_session"


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


def detect_agent() -> str:
    """Detect which AI agent is active."""
    if os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
    if os.environ.get("COPILOT_WORKSPACE"):
        return "copilot-workspace"
    return "unknown"


def save_session_id(session_id: str):
    """Save active session ID to file for persistence."""
    SESSION_FILE.write_text(session_id)


def load_session_id() -> str | None:
    """Load active session ID from file."""
    if SESSION_FILE.exists():
        content = SESSION_FILE.read_text().strip()
        return content if content else None
    return None


def clear_session_id():
    """Clear saved session ID."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()


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
            name="team_status",
            description="See what team members are currently working on. Shows active sessions and their scope.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="complete_session",
            description="Complete your session and release all locks. Call this when you're done working.",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Summary of what you accomplished",
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


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle MCP tool calls."""
    # Load active session from persistent storage
    active_session_id = load_session_id()

    async with httpx.AsyncClient(timeout=10.0) as client:
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
                        "agent": detect_agent(),
                        "scope": scope,
                        "description": description,
                        "branch": get_git_branch(),
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

                msg = f"✅ Session started!\n\n"
                msg += f"Session ID: {data['id'][:8]}...\n"
                msg += f"Scope: {', '.join(data['scope'])}\n"
                msg += f"Branch: {data['branch']}\n"
                msg += f"Locks created: {data['lock_count']}\n"
                msg += f"Mode: {'EXCLUSIVE (blocks all overlaps)' if exclusive else 'Advisory (warns on overlaps)'}\n\n"
                msg += "Team has been notified. Use complete_session when done."

                return [TextContent(type="text", text=msg)]

            elif name == "check_locks":
                paths = arguments["paths"]
                response = await client.post(
                    f"{SERVER_URL}/api/locks/check",
                    json={"paths": paths},
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
                    msg += f"   Mode: {r['mode']}\n\n"

                exclusive = [r for r in locked if r["mode"] == "exclusive"]
                if exclusive:
                    msg += "⛔ Exclusive locks block you. Use request_override to ask permission."
                else:
                    msg += "⚠️ Advisory locks - you can proceed but coordinate with team members."

                return [TextContent(type="text", text=msg)]

            elif name == "request_override":
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

                msg += f"Request ID: {data['id'][:8]}...\n"
                msg += f"Pattern: {data['conflicting_pattern']}\n"
                msg += f"Owner: {data['owner_developer']}\n"

                if not auto_decided:
                    msg += f"Expires: {data['expires_at']}\n\n"
                    msg += "💡 Tip: Use keywords 'urgent', 'security', 'hotfix', 'critical' for auto-approval\n"
                    msg += "Use check_my_override_requests to monitor response."

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

                if not requests:
                    return [TextContent(type="text", text="✅ No pending override requests.")]

                msg = f"📬 {len(requests)} pending override request(s) TO YOU:\n\n"
                for req in requests:
                    msg += f"Request ID: {req['id'][:8]}...\n"
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
                msg += f"Request ID: {data['id'][:8]}...\n"
                msg += f"Requester: {data['requester_developer']}\n"
                msg += f"Your message: {message}\n\n"
                msg += "Requester has been notified."

                return [TextContent(type="text", text=msg)]

            elif name == "team_status":
                response = await client.get(
                    f"{SERVER_URL}/api/sessions",
                    params={"status": "active"},
                )
                response.raise_for_status()
                sessions = response.json()

                if not sessions:
                    return [TextContent(type="text", text="✅ No active sessions. Team is available.")]

                msg = f"👥 {len(sessions)} active session(s):\n\n"
                for s in sessions:
                    scope = ", ".join(s["scope"]) if s["scope"] else "no scope"
                    msg += f"• {s['developer']} ({s['agent']})\n"
                    msg += f"  Scope: {scope}\n"
                    msg += f"  Branch: {s['branch']}\n"
                    msg += f"  Description: {s['description']}\n"
                    msg += f"  Locks: {s['lock_count']}  Decisions: {s['decision_count']}\n\n"

                return [TextContent(type="text", text=msg)]

            elif name == "complete_session":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session to complete.")]

                summary = arguments["summary"]
                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{active_session_id}",
                    json={"status": "completed", "summary": summary},
                )
                response.raise_for_status()

                msg = f"✅ Session completed!\n\n"
                msg += f"Summary: {summary}\n\n"
                msg += "All locks released. Team has been notified."

                clear_session_id()
                return [TextContent(type="text", text=msg)]

            elif name == "log_decision":
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
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No active session to pause.")]

                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{active_session_id}",
                    json={"status": "paused"},
                )
                response.raise_for_status()
                data = response.json()

                msg = f"⏸️ Session paused!\n\n"
                msg += f"Session ID: {data['id'][:8]}...\n"
                msg += f"Locks: {data['lock_count']} (retained)\n\n"
                msg += "Use resume_session to continue work."

                return [TextContent(type="text", text=msg)]

            elif name == "resume_session":
                if not active_session_id:
                    return [TextContent(type="text", text="❌ No session to resume.")]

                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{active_session_id}",
                    json={"status": "active"},
                )
                response.raise_for_status()
                data = response.json()

                msg = f"▶️ Session resumed!\n\n"
                msg += f"Session ID: {data['id'][:8]}...\n"
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
                msg += f"ID: {data['id'][:8]}...\n"
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

                    msg += f"{icon} Request ID: {req['id'][:8]}...\n"
                    msg += f"   To: {req['owner_developer']}\n"
                    msg += f"   Pattern: {req['conflicting_pattern']}\n"
                    msg += f"   Status: {req['status']}\n"

                    if req.get("response_message"):
                        msg += f"   Response: {req['response_message']}\n"

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

                in_scope = data.get("files_in_scope", [])
                out_scope = data.get("files_out_of_scope", [])

                if not in_scope and not out_scope:
                    return [TextContent(type="text", text="✅ No uncommitted changes.")]

                msg = ""

                if in_scope:
                    msg += f"📝 {len(in_scope)} uncommitted file(s) in your scope:\n\n"
                    for f in in_scope[:20]:  # Limit display
                        status = f.get("status", "modified")
                        msg += f"  {status}: {f['path']}\n"

                    if len(in_scope) > 20:
                        msg += f"  ... and {len(in_scope) - 20} more\n"
                    msg += "\n"

                if out_scope:
                    msg += f"⚠️ {len(out_scope)} file(s) outside your scope:\n\n"
                    for f in out_scope[:10]:
                        msg += f"  {f.get('status', 'modified')}: {f['path']}\n"

                    if len(out_scope) > 10:
                        msg += f"  ... and {len(out_scope) - 10} more\n"
                    msg += "\n💡 Consider expanding scope or creating new session\n"

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

                response = await client.post(
                    f"{SERVER_URL}/api/git/pre-commit-check",
                    json={"paths": paths},
                )
                response.raise_for_status()
                data = response.json()

                blocked = data.get("blocked", [])
                warned = data.get("warned", [])

                if not blocked and not warned:
                    return [TextContent(type="text", text="✅ All files clear for commit.")]

                msg = ""

                if blocked:
                    msg += f"⛔ {len(blocked)} file(s) BLOCKED by exclusive locks:\n\n"
                    for f in blocked:
                        msg += f"  {f['path']}\n"
                        msg += f"    Locked by: {f['developer']} (pattern: {f['pattern']})\n"
                    msg += "\n❌ Commit will be blocked. Resolve conflicts first.\n\n"

                if warned:
                    msg += f"⚠️ {len(warned)} file(s) have advisory locks:\n\n"
                    for f in warned:
                        msg += f"  {f['path']}\n"
                        msg += f"    Locked by: {f['developer']} (pattern: {f['pattern']})\n"
                    msg += "\n💡 Commit allowed but coordinate with team.\n"

                return [TextContent(type="text", text=msg)]

            # NEW TOOLS - Phase 4 (Polish)

            elif name == "delete_lock":
                lock_id = arguments["lock_id"]

                response = await client.delete(f"{SERVER_URL}/api/locks/{lock_id}")
                response.raise_for_status()

                msg = f"🗑️ Lock deleted!\n\n"
                msg += f"Lock ID: {lock_id[:8]}...\n\n"
                msg += "⚠️ Other team members have been notified."

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
                msg += f"ID: {data['id'][:8]}...\n"
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


if __name__ == "__main__":
    asyncio.run(main())
