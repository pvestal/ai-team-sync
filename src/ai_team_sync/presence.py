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
    last_seen: float = field(default_factory=time.time)


class PresenceStore:
    def __init__(self):
        self._devs: dict[str, DevPresence] = {}  # keyed by developer name
        self._connections: list[asyncio.Queue] = []

    def update(self, developer: str, agent: str, files: list[str]):
        self._devs[developer] = DevPresence(
            developer=developer, agent=agent, files=files, last_seen=time.time()
        )

    def remove(self, developer: str):
        self._devs.pop(developer, None)

    def get_all(self) -> list[dict]:
        self._evict()
        return [
            {"developer": d.developer, "agent": d.agent, "files": d.files}
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
