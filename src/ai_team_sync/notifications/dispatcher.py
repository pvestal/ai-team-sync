"""Notification dispatcher — fans out events to all configured adapters."""

from __future__ import annotations

import logging
from typing import Any

from ai_team_sync.config import settings

logger = logging.getLogger(__name__)

# Adapters are registered at import time
_adapters: list[Any] = []


def _init_adapters():
    """Lazily initialize notification adapters based on config."""
    if _adapters:
        return

    if settings.slack_webhook_url:
        from ai_team_sync.notifications.slack import SlackAdapter
        _adapters.append(SlackAdapter(settings.slack_webhook_url))
        logger.info("Slack notifications enabled")

    if settings.telegram_bot_token and settings.telegram_chat_id:
        from ai_team_sync.notifications.telegram import TelegramAdapter
        _adapters.append(TelegramAdapter(settings.telegram_bot_token, settings.telegram_chat_id))
        logger.info("Telegram notifications enabled")


async def dispatch(event: str, data: dict[str, Any]):
    """Send a notification event to all configured adapters."""
    _init_adapters()

    if not _adapters:
        logger.debug("No notification adapters configured, skipping event: %s", event)
        return

    message = format_message(event, data)

    for adapter in _adapters:
        try:
            await adapter.send(event, message, data)
        except Exception:
            logger.exception("Failed to send %s via %s", event, type(adapter).__name__)


def format_message(event: str, data: dict[str, Any]) -> str:
    """Format a human-readable notification message."""
    dev = data.get("developer", "Someone")
    agent = data.get("agent", "an AI agent")

    match event:
        case "session.started":
            scope = ", ".join(data.get("scope", [])) or "unspecified scope"
            desc = data.get("description", "")
            msg = f"{dev} started working on `{scope}` with {agent}"
            if desc:
                msg += f"\n> {desc}"
            return msg

        case "session.completed":
            branch = data.get("branch", "")
            summary = data.get("summary", "")
            msg = f"{dev} completed their session"
            if branch:
                msg += f" (branch: `{branch}`)"
            if summary:
                msg += f"\n> {summary}"
            return msg

        case "lock.conflict":
            paths = ", ".join(data.get("paths", []))
            pattern = data.get("pattern", "")
            return f"CONFLICT: Attempted edit to `{paths}` — locked by {dev} (pattern: `{pattern}`)"

        case "lock.expired":
            pattern = data.get("pattern", "")
            return f"{dev}'s lock on `{pattern}` has expired"

        case "decision.logged":
            title = data.get("title", "Untitled")
            chosen = data.get("chosen", "")
            rejected = data.get("rejected", "")
            msg = f"Decision by {dev}: **{title}**\nChose: {chosen}"
            if rejected:
                msg += f"\nRejected: {rejected}"
            return msg

        case _:
            return f"[{event}] {data}"
