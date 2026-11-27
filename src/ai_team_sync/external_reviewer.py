"""Identity markers and presentation for connector-authenticated review notes."""

import unicodedata

AGENT = "chatgpt-connector"
ACTOR = "oauth:tower-chatgpt"
CAPABILITY_HEADER = "X-ATS-External-Message-Capability"
TOKEN_PREFIX = "ats-external-note-"
MESSAGE_PATH = "/api/external-review/messages"


def external_session(session) -> bool:
    return session.agent == AGENT or session.developer == ACTOR


def clean_body(body: str) -> str:
    body = body.strip()
    if not body or any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in body):
        raise ValueError("Message must be nonempty plain text without control characters")
    return body


def inbox_body(row: dict, limit: int | None = None) -> str:
    body = row["body"] if limit is None else row["body"][:limit]
    if row.get("sender_agent") == AGENT:
        quoted = "\n".join(f"    > {line}" for line in body.splitlines())
        return f"\n  External engineering note from {ACTOR} ({row.get('kind', 'message')})\n{quoted}"
    return body
