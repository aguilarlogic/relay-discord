from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from relay.db import Database
from relay.kb import KnowledgeBase


@pytest.fixture
async def db():
    database = await Database.open(":memory:")
    yield database
    await database.close()


@pytest.fixture
async def kb(db):
    return KnowledgeBase(db)


@dataclass
class FakeMessages:
    """Stands in for AsyncAnthropic().messages: returns queued responses and
    records every request."""

    responses: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, *responses: Any) -> None:
        self.messages = FakeMessages(list(responses))


def json_response(data: Any, stop_reason: str = "end_turn", input_tokens: int = 1000, output_tokens: int = 200) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=json.dumps(data)),
        ],
    )


TIERS_TOML = """
[[tier]]
key = "free"
name = "Free"
monthly_answers = 3
help_channels = 1
llm = "free"

[[tier]]
key = "starter"
name = "Starter"
sku_id = 101
price_label = "$4.99/month"
monthly_price_usd = 4.99
monthly_answers = 10
help_channels = 2
llm = "claude-haiku-4-5"
kb_upload = true

[[tier]]
key = "pro"
name = "Pro"
sku_id = 102
price_label = "$19.99/month"
monthly_price_usd = 19.99
monthly_answers = "unlimited"
help_channels = "unlimited"
llm = "claude-sonnet-5"
digest = true
kb_upload = true

[[topup]]
sku_id = 201
name = "+5 answers"
price_label = "$1"
answers = 5

[prices]
"free" = { input = 0.0, output = 0.0 }
"claude-haiku-4-5" = { input = 1.0, output = 5.0 }
"claude-sonnet-5" = { input = 2.0, output = 10.0 }
"""


@pytest.fixture
def tiers(tmp_path):
    from relay.tiers import load_tiers

    path = tmp_path / "tiers.toml"
    path.write_text(TIERS_TOML)
    return load_tiers(path)


class FakeRouter:
    """Returns one FakeAnthropic-backed ClaudeLLM for every tier key, and
    records which keys were requested."""

    def __init__(self, *responses: Any) -> None:
        from relay.llm import ClaudeLLM

        self.client = FakeAnthropic(*responses)
        self.requested: list[str] = []
        self._llm = ClaudeLLM(self.client, "claude-sonnet-5")

    def get(self, llm: str):
        self.requested.append(llm)
        return self._llm
