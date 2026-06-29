"""Configuration via environment variables and TOML files."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Server and integration settings, loaded from environment."""

    database_url: str = "sqlite+aiosqlite:///ai_team_sync.db"

    # Server
    ats_host: str = "0.0.0.0"
    ats_port: int = 8400

    # Slack
    slack_webhook_url: str | None = None

    # Telegram
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Locks
    lock_ttl_hours: int = 8
    lock_default_mode: str = "advisory"

    # Sessions: FALLBACK auto-complete window for sessions that never heartbeat
    # (legacy clients / MCP-only flows). After this many hours with no derived
    # activity (no new locks/commits/decisions) the session is completed so it stops
    # holding its lane. Heartbeating sessions use the much faster path below; this is
    # only the safety net. 4h trades a slightly higher chance of reaping a genuinely
    # idle-but-live non-heartbeating session for not leaving dead lanes parked all
    # day — wire the heartbeat hook and the fast path makes this moot.
    session_inactivity_hours: int = 4

    # Fast reaper path for sessions that emit a liveness heartbeat. A session that
    # has EVER heartbeated and then goes silent (process died) for this many minutes
    # with no newer lock/commit/decision is auto-completed well before the
    # session_inactivity_hours fallback. Only applies once last_heartbeat is set, so
    # non-heartbeating clients are unaffected. Keep comfortably above the client
    # heartbeat cadence (a per-turn Stop hook) so a slow-but-live session is never
    # falsely reaped.
    session_heartbeat_timeout_minutes: int = 20

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_team_config(repo_root: Path | None = None) -> dict[str, Any]:
    """Load .ai-team-sync.toml from the repo root, if it exists."""
    if repo_root is None:
        repo_root = Path.cwd()
    config_path = repo_root / ".ai-team-sync.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


settings = Settings()
