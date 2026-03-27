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
