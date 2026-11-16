#!/usr/bin/env python3
"""PreToolUse hook for ATS MCP tools: record WHICH agent is calling (#3677).

Every subagent of one Claude session shares that session's id and its one
stdio MCP process, so the MCP server cannot tell the main thread from a
subagent: a subagent's start_session used to take over the parent's pointer and
default session. Claude Code names the caller in the hook payload instead
(`agent_id`, present only inside a subagent) and sends the same `tool_use_id`
to the MCP server as `_meta["claudecode/toolUseId"]`. This hook writes
tool_use_id -> agent_id (session_pointer.record_tool_caller) before the call
runs, and the MCP server reads it for that call.

It never blocks and prints nothing: a missing record only means the MCP server
treats the caller as unknown, which is the behaviour before this hook existed.

Wire (~/.claude/settings.json; scripts/install-claude-hooks.py does this):
  "PreToolUse": [{ "matcher": "mcp__ai-team-sync__.*",
    "hooks": [{ "type": "command",
      "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_ats_caller" }] }]
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)
    tool_use_id = str(payload.get("tool_use_id") or "")
    cid = str(payload.get("session_id") or "")
    agent_id = payload.get("agent_id")
    agent_id = agent_id if isinstance(agent_id, str) else ""
    try:
        from ai_team_sync import session_pointer as sp

        sp.record_tool_caller(tool_use_id, cid, agent_id)
    except Exception:
        pass  # best-effort: an unknown caller is the pre-#3677 behaviour
    sys.exit(0)


if __name__ == "__main__":
    main()
