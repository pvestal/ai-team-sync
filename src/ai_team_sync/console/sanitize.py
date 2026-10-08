"""Last-mile display sanitization for external read-only sources."""

from __future__ import annotations

import re
from typing import Any

_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRETS = (
    re.compile(r"(?i)\b(?:sk-|xox[baprs]-|gh[pousr]_)[a-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|redis)://[^\s]+"),
    re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/-]+=*"),
    re.compile(r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"),
)


def safe_text(value: Any, limit: int = 500) -> str:
    text = _CONTROL.sub("", _ANSI.sub("", str(value or "")))
    for pattern in _SECRETS:
        text = pattern.sub("[REDACTED]", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def safe_value(value: Any, *, depth: int = 0) -> Any:
    """Sanitize nested API values and cap their shape for terminal rendering."""
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {
            safe_text(key, 100): safe_value(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, list):
        return [safe_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return safe_text(value)
