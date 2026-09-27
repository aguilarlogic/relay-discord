"""Thin wrapper for structured (JSON-schema) calls to the Claude API."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# Models that accept the server-side `fallbacks` parameter. On these, a request
# declined by a safety classifier is re-run on Anthropic's recommended fallback
# model inside the same call instead of coming back as a refusal.
_FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-fable-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Models that accept output_config.effort.
_EFFORT_MODEL_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5", "claude-opus-4-")


class LLMError(RuntimeError):
    """The call failed (API error, refusal, or unusable output). Callers treat
    this as "no answer" -- the bot stays quiet rather than posting junk."""


class StructuredLLM:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        *,
        effort: str | None = None,
        refusal_fallback: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.refusal_fallback = refusal_fallback

    def _request_kwargs(self, schema: dict[str, Any]) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort and self.model.startswith(_EFFORT_MODEL_PREFIXES):
            output_config["effort"] = self.effort
        kwargs: dict[str, Any] = {"output_config": output_config}
        if self.refusal_fallback and self.model.startswith(_FALLBACK_MODEL_PREFIXES):
            kwargs["extra_headers"] = {"anthropic-beta": _FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}
        return kwargs

    async def call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8000) -> dict[str, Any]:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                **self._request_kwargs(schema),
            )
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited by the Claude API") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("could not reach the Claude API") from e

        if response.stop_reason == "refusal":
            raise LLMError("request was declined")
        if response.stop_reason == "max_tokens":
            raise LLMError("response was cut off at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("response had no text block")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("response was not valid JSON") from e
        if not isinstance(data, dict):
            raise LLMError("response JSON was not an object")
        return data
