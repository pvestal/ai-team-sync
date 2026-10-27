"""Configuration via environment variables and TOML files."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


def _ats_env_file() -> Path:
    """The one env file ATS reads: $ATS_ENV_FILE, else ~/.config/ai-team-sync/ats.env.

    Never a bare ".env": that resolves against the caller's CWD, so running `ats`
    inside another repo loaded that repo's secrets (#3586).
    """
    explicit = os.environ.get("ATS_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".config" / "ai-team-sync" / "ats.env"


class Settings(BaseSettings):
    """Server and integration settings, loaded from environment."""

    database_url: str = "sqlite+aiosqlite:///ai_team_sync.db"

    # Server
    ats_host: str = "127.0.0.1"
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

    # Keys ATS does not declare are ignored, and validation errors never echo
    # the rejected value (#3586).
    model_config = SettingsConfigDict(
        env_file=_ats_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


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
