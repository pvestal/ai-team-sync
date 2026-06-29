"""In-memory file presence — who has what open right now."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

STALE_SECONDS = 30


@dataclass
class DevPresence:
    developer: str
    agent: str
    files: list[str] = field(default_factory=list)
    intent: str = ""  # one-line WHAT they're doing ("rewriting token validation")
    last_seen: float = field(default_factory=time.time)


class PresenceStore:
    def __init__(self):
        # Keyed by (developer, agent), NOT developer alone: one operator runs several
        # agent sessions under the same git user (agent = per-session label like
        # 'claude-code:ab12cd34'). Keying by developer made concurrent same-developer
        # sessions clobber each other's presence, so whos_editing went blind to a
        # parallel session of the same person. The composite key keeps them distinct.
        self._devs: dict[tuple[str, str], DevPresence] = {}
        self._connections: list[asyncio.Queue] = []

    def update(self, developer: str, agent: str, files: list[str], intent: str = ""):
        self._devs[(developer, agent)] = DevPresence(
            developer=developer, agent=agent, files=files, intent=intent, last_seen=time.time()
        )

    def remove(self, developer: str, agent: str | None = None):
        """Remove one session's presence, or all of a developer's if agent is None
        (WS disconnect knows only the developer)."""
        if agent is None:
            self._devs = {k: v for k, v in self._devs.items() if k[0] != developer}
        else:
            self._devs.pop((developer, agent), None)

    def get_all(self) -> list[dict]:
        self._evict()
        return [
            {"developer": d.developer, "agent": d.agent, "files": d.files, "intent": d.intent}
            for d in self._devs.values()
            if d.files
        ]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._connections.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._connections = [c for c in self._connections if c is not q]

    async def broadcast(self):
        snapshot = self.get_all()
        for q in self._connections:
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass

    def _evict(self):
        cutoff = time.time() - STALE_SECONDS
        self._devs = {k: v for k, v in self._devs.items() if v.last_seen > cutoff}


store = PresenceStore()
