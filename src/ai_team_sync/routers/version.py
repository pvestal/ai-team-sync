"""Build identity of the running REST service."""

from __future__ import annotations

from fastapi import APIRouter

from ai_team_sync.build_info import identity

router = APIRouter(prefix="/version", tags=["version"])


@router.get("")
async def get_version() -> dict:
    return identity("ats-rest")
