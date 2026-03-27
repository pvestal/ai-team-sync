"""MCP Server for ai-team-sync integration with Claude Code."""

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


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
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
            description="Request permission to work on files locked by someone else. Use when blocked by exclusive lock.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The lock pattern blocking you (e.g., 'backend/**')",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Why you need access (e.g., 'Urgent security fix needed')",
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
    ]


# Store active session ID
_active_session_id: str | None = None


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle MCP tool calls."""
    global _active_session_id

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            if name == "start_session":
                # Create session
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
                    # Conflict detected
                    error = response.json()
                    conflicts = error["detail"].get("conflicts", [])
                    msg = f"❌ Cannot start session - conflicts detected:\n\n"
                    for c in conflicts:
                        msg += f"  • Pattern '{c['new_pattern']}' conflicts with '{c['existing_pattern']}'\n"
                        msg += f"    Held by: {c['existing_developer']} ({c['lock_mode']} lock)\n\n"
                    msg += "Use request_override tool to ask for permission."
                    return [TextContent(type="text", text=msg)]

                response.raise_for_status()
                data = response.json()
                _active_session_id = data["id"]

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
                if not _active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Start a session first with start_session.")]

                pattern = arguments["pattern"]
                justification = arguments["justification"]

                response = await client.post(
                    f"{SERVER_URL}/api/override-requests",
                    json={
                        "requester_session_id": _active_session_id,
                        "conflicting_pattern": pattern,
                        "justification": justification,
                    },
                )
                response.raise_for_status()
                data = response.json()

                msg = f"✅ Override request sent!\n\n"
                msg += f"Request ID: {data['id'][:8]}...\n"
                msg += f"Pattern: {data['conflicting_pattern']}\n"
                msg += f"Owner: {data['owner_developer']}\n"
                msg += f"Expires: {data['expires_at']}\n\n"
                msg += "Lock owner has been notified. Use check_pending_requests to monitor response."

                return [TextContent(type="text", text=msg)]

            elif name == "check_pending_requests":
                if not _active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Start a session first.")]

                response = await client.get(
                    f"{SERVER_URL}/api/override-requests",
                    params={"session_id": _active_session_id, "status": "pending"},
                )
                response.raise_for_status()
                requests = response.json()

                if not requests:
                    return [TextContent(type="text", text="✅ No pending override requests.")]

                msg = f"📬 {len(requests)} pending override request(s):\n\n"
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
                if not _active_session_id:
                    return [TextContent(type="text", text="❌ No active session to complete.")]

                summary = arguments["summary"]
                response = await client.patch(
                    f"{SERVER_URL}/api/sessions/{_active_session_id}",
                    json={"status": "completed", "summary": summary},
                )
                response.raise_for_status()

                msg = f"✅ Session completed!\n\n"
                msg += f"Summary: {summary}\n\n"
                msg += "All locks released. Team has been notified."

                _active_session_id = None
                return [TextContent(type="text", text=msg)]

            elif name == "log_decision":
                if not _active_session_id:
                    return [TextContent(type="text", text="❌ No active session. Start a session first.")]

                title = arguments["title"]
                chosen = arguments["chosen"]
                rejected = arguments.get("rejected", "")
                reasoning = arguments["reasoning"]

                response = await client.post(
                    f"{SERVER_URL}/api/decisions",
                    json={
                        "session_id": _active_session_id,
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

            else:
                return [TextContent(type="text", text=f"❌ Unknown tool: {name}")]

        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"❌ HTTP Error: {e.response.status_code}\n{e.response.text}")]
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
