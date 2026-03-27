"""Slack webhook notification adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SlackAdapter:
    """Sends notifications to a Slack channel via incoming webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def send(self, event: str, message: str, data: dict[str, Any]):
        payload = {
            "text": message,
            "unfurl_links": False,
            "unfurl_media": False,
        }

        # Add a color bar based on event type
        color_map = {
            "session.started": "#2196F3",   # blue
            "session.completed": "#4CAF50", # green
            "lock.conflict": "#F44336",     # red
            "lock.expired": "#FF9800",      # orange
            "decision.logged": "#9C27B0",   # purple
        }

        if event in color_map:
            payload = {
                "attachments": [{
                    "color": color_map[event],
                    "text": message,
                    "footer": f"ai-team-sync | {event}",
                }]
            }

        async with httpx.AsyncClient() as client:
            resp = await client.post(self.webhook_url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error("Slack webhook failed: %s %s", resp.status_code, resp.text)
