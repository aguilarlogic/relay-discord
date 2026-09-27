"""Typed settings loaded from the environment (and .env)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str
    # Optional here because the Anthropic SDK resolves credentials on its own
    # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
    anthropic_api_key: str | None = None

    relay_model: str = "claude-opus-5"
    relay_fast_model: str = "claude-haiku-4-5"
    # Thinking depth for answers. Support answers are short retrieval tasks, so
    # "medium" keeps latency and cost down without hurting grounded answers.
    relay_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Re-run a safety-declined answer on Anthropic's recommended fallback model
    # instead of silently declining. Only sent for models that support it.
    relay_refusal_fallback: bool = True

    database_path: Path = Path("data/relay.db")
    premium_sku_id: int | None = None
    dev_guild_id: int | None = None

    @field_validator("premium_sku_id", "dev_guild_id", "anthropic_api_key", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # .env.example ships these as `KEY=` -- treat blank as unset.
        if isinstance(v, str) and not v.strip():
            return None
        return v
