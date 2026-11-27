"""Write bounded external-review notes through the existing ATS message API."""

import json
import os
from pathlib import Path
import stat
import uuid

from mcp.types import TextContent, Tool, ToolAnnotations

from ai_team_sync.external_reviewer import ACTOR, AGENT, CAPABILITY_HEADER, MESSAGE_PATH, TOKEN_PREFIX

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


def _sender() -> tuple[str, str]:
    state = Path(os.environ.get("ATS_STATE_DIR") or Path.home() / ".local/state/ats-mcp-remote")
    with os.fdopen(os.open(state / "chatgpt-ats-sender.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("External ATS sender credential must be a private regular file")
        record = json.loads(stream.read(2048))
    token = record["token"]
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or not 32 <= len(token) <= 128 \
            or not token.isascii() or not token.isprintable():
        raise ValueError("Invalid external ATS sender capability")
    return str(uuid.UUID(record["session_id"])), token


async def send_note(client, base: str, args: dict, request_id: str) -> list[TextContent]:
    target = str(uuid.UUID(args["recipient_session_id"]))
    body = args["body"].strip()
    if not body:
        raise ValueError("Engineering note is empty")
    sender, token = _sender()
    identity = await client.get(f"{base}/api/sessions/{sender}")
    identity.raise_for_status()
    row = identity.json()
    if (row.get("agent"), row.get("developer"), row.get("status")) != (AGENT, ACTOR, "active"):
        raise PermissionError("Configured ATS sender is not the external ChatGPT session")
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
        "actor": ACTOR, "target": target, "request_id": request_id,
        "message_id": message, "outcome": "recorded",
    }))]
