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

    # Model for the daily digest's topic clustering (paid tiers only).
    relay_fast_model: str = "claude-haiku-4-5"
    # Thinking depth for answers on Claude models that support it. Support
    # answers are short retrieval tasks, so "medium" keeps latency and cost down.
    relay_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Re-run a safety-declined answer on Anthropic's recommended fallback model
    # instead of silently declining. Only sent for models that support it.
    relay_refusal_fallback: bool = True
    # Plans, limits, prices, and each tier's AI model.
    relay_tiers_path: Path = Path("tiers.toml")

    # The free tier's AI: any OpenAI-compatible chat endpoint. Defaults to
    # Google Gemini's OpenAI-compatible API (free quota via an AI Studio key).
    free_llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    free_llm_api_key: str | None = None
    free_llm_model: str | None = None
    # How to ask for JSON: "json_schema" (strict structured output), "json_object"
    # (JSON mode), or "none" (prompt only). Use whatever the provider supports.
    free_llm_json_mode: Literal["json_schema", "json_object", "none"] = "json_schema"

    database_path: Path = Path("data/relay.db")
    dev_guild_id: int | None = None

    @field_validator("dev_guild_id", "anthropic_api_key", "free_llm_api_key", "free_llm_model", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # .env.example ships these as `KEY=` -- treat blank as unset.
        if isinstance(v, str) and not v.strip():
            return None
        return v
