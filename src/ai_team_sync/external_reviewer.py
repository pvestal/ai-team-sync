"""Identity markers and presentation for connector-authenticated review notes."""

import unicodedata

REVIEWERS = {
    "oauth:tower-chatgpt": "chatgpt-connector",
    "oauth:tower-claude": "claude-ai-connector",
}
CAPABILITY_HEADER = "X-ATS-External-Message-Capability"
TOKEN_PREFIX = "ats-external-note-"
MESSAGE_PATH = "/api/external-review/messages"
INBOX_PATH = "/api/external-review/inbox"
CAPABILITY_PATHS = frozenset({MESSAGE_PATH, INBOX_PATH})


def external_session(session) -> bool:
    return session.agent in REVIEWERS.values() or session.developer in REVIEWERS


def reviewer_identity(agent: str, developer: str) -> bool:
    return developer in REVIEWERS and REVIEWERS[developer] == agent


def clean_body(body: str) -> str:
    body = body.strip()
    if not body or any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in body):
        raise ValueError("Message must be nonempty plain text without control characters")
    return body


def inbox_body(row: dict, limit: int | None = None) -> str:
    body = row["body"] if limit is None else row["body"][:limit]
    actor = row.get("sender_developer")
    if reviewer_identity(row.get("sender_agent"), actor):
        return f"\n  External engineering note from {actor} ({row.get('kind', 'message')})\n{body}"
    return body


def quoted_body(body: str) -> str:
    return "\n".join(f"    > {line}" for line in body.splitlines())
