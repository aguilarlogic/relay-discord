"""Structured (JSON) LLM calls behind one interface, for two kinds of provider:

- ClaudeLLM: the Claude API, used by paid tiers.
- OpenAICompatLLM: any OpenAI-compatible chat endpoint (Google Gemini's free
  API, Groq, OpenRouter, ...), used by the free tier.

Every call returns token usage so the bot can log what each server costs.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import aiohttp
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
    """The call failed (API error, refusal, quota, or unusable output). Callers
    treat this as "no answer" -- the bot stays quiet rather than posting junk."""


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    model: str

    async def call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8000) -> LLMResult: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating the ```json fences
    and stray prose that providers without strict structured output add."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("response was not valid JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError("response was not valid JSON") from e
    if not isinstance(data, dict):
        raise LLMError("response JSON was not an object")
    return data


class ClaudeLLM:
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

    async def call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8000) -> LLMResult:
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
        usage = getattr(response, "usage", None)
        return LLMResult(
            data=parse_json_object(text),
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )


JsonMode = Literal["json_schema", "json_object", "none"]


class OpenAICompatLLM:
    """POST {base_url}/chat/completions. Works with Gemini's OpenAI-compatible
    endpoint, Groq, OpenRouter, a local Ollama, etc."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        json_mode: JsonMode = "json_schema",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.session = session
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.json_mode = json_mode
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def build_payload(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        # Always describe the schema in the prompt too: not every provider
        # enforces response_format, and json_object mode requires the word JSON.
        system = f"{system}\n\nRespond with only a JSON object matching this JSON Schema:\n{json.dumps(schema)}"
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }
        elif self.json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 2000) -> LLMResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = self.build_payload(system=system, user=user, schema=schema, max_tokens=max_tokens)
        try:
            async with self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout) as resp:
                if resp.status == 429:
                    raise LLMError("free AI provider quota or rate limit reached")
                if resp.status >= 400:
                    raise LLMError(f"free AI provider error {resp.status}")
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise LLMError("could not reach the free AI provider") from e
        except ValueError as e:  # 2xx with a non-JSON body
            raise LLMError("free AI provider returned a non-JSON response") from e

        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("free AI provider returned an unexpected response") from e
        if choice.get("finish_reason") == "length":
            raise LLMError("response was cut off at max_tokens")
        usage = body.get("usage") or {}
        return LLMResult(
            data=parse_json_object(text),
            model=self.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


class LLMRouter:
    """Hands out the client for a tier's `llm` setting: "free" -> the
    OpenAI-compatible free provider, anything else -> that Claude model."""

    def __init__(
        self,
        anthropic_client: anthropic.AsyncAnthropic,
        free_llm: OpenAICompatLLM | None,
        *,
        effort: str | None = None,
        refusal_fallback: bool = True,
    ) -> None:
        self.anthropic_client = anthropic_client
        self.free_llm = free_llm
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self._claude: dict[str, ClaudeLLM] = {}

    def get(self, llm: str) -> LLMClient:
        if llm == "free":
            if self.free_llm is None:
                raise LLMError("the free AI provider is not configured (set FREE_LLM_MODEL and FREE_LLM_API_KEY)")
            return self.free_llm
        if llm not in self._claude:
            self._claude[llm] = ClaudeLLM(
                self.anthropic_client, llm, effort=self.effort, refusal_fallback=self.refusal_fallback
            )
        return self._claude[llm]
