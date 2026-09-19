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
    session_id: str = ""
    files: list[str] = field(default_factory=list)
    intent: str = ""  # one-line WHAT they're doing ("rewriting token validation")
    last_seen: float = field(default_factory=time.time)


class PresenceStore:
    def __init__(self):
        # A session id distinguishes two workers of the same type and account.
        # Legacy clients without one retain their (developer, agent) key.
        self._devs: dict[tuple[str, str], DevPresence] = {}
        self._connections: list[asyncio.Queue] = []

    def update(self, developer: str, agent: str, files: list[str], intent: str = "",
               session_id: str = ""):
        key = ("session", session_id) if session_id else (developer, agent)
        self._devs[key] = DevPresence(
            developer=developer, agent=agent, session_id=session_id,
            files=files, intent=intent, last_seen=time.time()
        )

    def remove(self, developer: str, agent: str | None = None,
               session_id: str = ""):
        """Remove exactly one connection/session when its identity is known."""
        if session_id:
            self._devs.pop(("session", session_id), None)
        elif agent is None:
            self._devs = {k: v for k, v in self._devs.items() if k[0] != developer}
        else:
            self._devs.pop((developer, agent), None)

    def get_all(self) -> list[dict]:
        self._evict()
        return [
            {"developer": d.developer, "agent": d.agent, "session_id": d.session_id,
             "files": d.files, "intent": d.intent}
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
