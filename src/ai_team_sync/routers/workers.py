"""Worker registry endpoints — how a worker discovers what it may do.

Read-only and unauthenticated like the rest of the local API. The registry is
declared in config, not negotiated by the caller: asking does not grant.
"""

from __future__ import annotations

from fastapi import APIRouter

from ai_team_sync.workers import registry

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("")
async def list_workers() -> list[dict]:
    return [w.as_dict() for w in registry().all()]


@router.get("/{label:path}")
async def get_worker(label: str) -> dict:
    """Resolve any session label ('claude-code:fb0bb6bf', 'local:qwen3-30b') to
    the worker that governs it. An unregistered label resolves to least
    privilege rather than 404 — the answer to "what may I do" is never nothing."""
    return registry().resolve(label).as_dict()
