"""WebSocket endpoint for real-time file presence."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ai_team_sync.presence import store

router = APIRouter()


@router.websocket("/ws/presence")
async def presence_ws(ws: WebSocket):
    await ws.accept()
    queue = store.subscribe()
    developer = None

    async def reader():
        nonlocal developer
        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                if msg.get("type") == "presence":
                    developer = msg["developer"]
                    store.update(msg["developer"], msg.get("agent", "?"), msg.get("files", []))
                    await store.broadcast()
        except (WebSocketDisconnect, Exception):
            pass

    async def writer():
        try:
            # Send initial state
            await ws.send_text(json.dumps({"type": "update", "presence": store.get_all()}))
            while True:
                snapshot = await queue.get()
                await ws.send_text(json.dumps({"type": "update", "presence": snapshot}))
        except (WebSocketDisconnect, Exception):
            pass

    read_task = asyncio.create_task(reader())
    write_task = asyncio.create_task(writer())

    try:
        await asyncio.gather(read_task, write_task)
    finally:
        store.unsubscribe(queue)
        if developer:
            store.remove(developer)
            await store.broadcast()
