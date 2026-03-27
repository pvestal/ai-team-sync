"""Telegram Bot API notification adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramAdapter:
    """Sends notifications to a Telegram chat via Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, event: str, message: str, data: dict[str, Any]):
        # Prepend an emoji based on event type
        emoji_map = {
            "session.started": "\U0001f7e2",   # green circle
            "session.completed": "\u2705",      # check mark
            "lock.conflict": "\U0001f6a8",      # rotating light
            "lock.expired": "\u23f0",           # alarm clock
            "decision.logged": "\U0001f4dd",    # memo
        }
        prefix = emoji_map.get(event, "\U0001f514")  # bell default
        text = f"{prefix} *ai-team-sync*\n\n{message}"

        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error("Telegram send failed: %s %s", resp.status_code, resp.text)
