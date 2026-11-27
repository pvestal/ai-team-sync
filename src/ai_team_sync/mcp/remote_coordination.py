"""Write bounded external-review notes through the existing ATS message API."""

import json
import os
from pathlib import Path
import re
import stat
import uuid

from mcp.types import TextContent, Tool, ToolAnnotations

from ai_team_sync.external_reviewer import (
    CAPABILITY_HEADER, INBOX_PATH, MESSAGE_PATH, REVIEWERS, TOKEN_PREFIX,
)
from .remote_tower import _tool, scrub

TOOL = Tool(
    name="send_engineering_note",
    description="Send an attributable engineering coordination note or review finding to an active ATS session.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
    inputSchema={
        "type": "object", "additionalProperties": False,
        "required": ["recipient_session_id", "request_id", "kind", "body"],
        "properties": {
            "recipient_session_id": {"type": "string", "format": "uuid", "maxLength": 36},
            "request_id": {"type": "string", "format": "uuid", "maxLength": 36,
                           "description": "Reuse this ID for an exact retry."},
            "kind": {"type": "string", "enum": ["coordination", "review_finding"]},
            "body": {"type": "string", "minLength": 1, "maxLength": 3000},
        },
    },
)
INBOX_TOOL = _tool(
    "engineering_messages",
    "Read your own ATS inbox, sent notes with delivery status, or conversation history.",
    {
        "box": {"type": "string", "enum": ["incoming", "sent", "conversation"]},
        "with_session_id": {"type": "string", "format": "uuid", "maxLength": 36},
        "pending_only": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
)


def _sender(actor: str) -> tuple[str, str]:
    state = Path(os.environ.get("ATS_STATE_DIR") or Path.home() / ".local/state/ats-mcp-remote")
    name = REVIEWERS[actor].removesuffix("-connector") + "-ats-sender.json"
    with os.fdopen(os.open(state / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("External ATS sender credential must be a private regular file")
        record = json.loads(stream.read(2048))
    token = record["token"]
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or not 32 <= len(token) <= 128 \
            or not token.isascii() or not token.isprintable():
        raise ValueError("Invalid external ATS sender capability")
    return str(uuid.UUID(record["session_id"])), token


async def _bound_sender(client, base: str, actor: str) -> tuple[str, str]:
    sender, token = _sender(actor)
    identity = await client.get(f"{base}/api/sessions/{sender}")
    identity.raise_for_status()
    row = identity.json()
    if (row.get("agent"), row.get("developer"), row.get("status")) != (REVIEWERS[actor], actor, "active"):
        raise PermissionError("Configured ATS sender does not match this external reviewer")
    return sender, token


async def send_note(client, base: str, actor: str, args: dict, request_id: str) -> list[TextContent]:
    target = str(uuid.UUID(args["recipient_session_id"]))
    body = args["body"].strip()
    if not body:
        raise ValueError("Engineering note is empty")
    sender, token = await _bound_sender(client, base, actor)
    response = await client.post(
        f"{base}{MESSAGE_PATH}",
        headers={CAPABILITY_HEADER: token},
        json={
            "sender_session_id": sender, "recipient_session_id": target,
            "request_id": request_id, "kind": args["kind"], "body": body,
        },
    )
    response.raise_for_status()
    message = str(uuid.UUID(response.json()["id"]))
    if message != request_id:
        raise RuntimeError("ATS message receipt does not match the request")
    return [TextContent(type="text", text=json.dumps({
        "actor": actor, "target": target, "request_id": request_id,
        "message_id": message, "outcome": "recorded",
    }))]


_INBOX_FIELDS = (
    "id", "sender_agent", "sender_developer", "sender_session_id", "recipient_agent",
    "recipient_session_id", "kind", "body", "created_at", "acknowledged_at",
    "first_surfaced_at", "last_surfaced_at", "surface_count",
)
_NOTE_URL = re.compile(r"https?://[^\s<>\x22]+", re.IGNORECASE)
_REPO_URL = re.compile(
    r"https://github\.com/pvestal/(?:anime-studio|ai-team-sync|tower-echo-brain)"
    r"(?:/(?:commit|pull|issues)/[0-9a-f]+)?/?"
)


def _external_body(body: str) -> str:
    text = scrub(body)
    text = _NOTE_URL.sub(lambda match: match[0] if _REPO_URL.fullmatch(match[0]) else "<link>", text)
    return text[:1500]


async def read_inbox(client, base: str, actor: str, args: dict) -> list[TextContent]:
    session, token = await _bound_sender(client, base, actor)
    response = await client.post(f"{base}{INBOX_PATH}", headers={CAPABILITY_HEADER: token},
                                 json={**args, "session_id": session})
    response.raise_for_status()
    rows = [{key: row.get(key) for key in _INBOX_FIELDS} for row in response.json()]
    for row in rows:
        row["body"] = _external_body(row["body"])
    return [TextContent(type="text", text=json.dumps({"actor": actor, "session_id": session,
                                                     "messages": rows}))]
