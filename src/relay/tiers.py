"""Subscription tiers and top-up packs, loaded from tiers.toml.

Keeping these in a config file means prices, limits, and which AI model each
tier uses can change without a code change. Actual prices are set on the
SKUs in the Discord Developer Portal; `price_label` / `monthly_price_usd`
here are only for display and for the `relay costs` margin report.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator

FREE_LLM = "free"  # sentinel: use the FREE_LLM_* OpenAI-compatible provider


def _unlimited(v: object) -> object:
    # TOML has no null, so the config spells "no limit" as "unlimited".
    return None if v == "unlimited" else v


def _positive(v: int | None) -> int | None:
    if v is not None and v < 1:
        raise ValueError('must be at least 1 (use "unlimited" for no limit)')
    return v


Limit = Annotated[int | None, BeforeValidator(_unlimited), AfterValidator(_positive)]


class Tier(BaseModel):
    key: str
    name: str
    sku_id: int | None = None  # None/0 = not purchasable (the free tier, or a tier not set up yet)
    price_label: str = ""
    monthly_price_usd: float = 0.0
    monthly_answers: Limit
    help_channels: Limit
    llm: str  # "free" or a Claude model id
    digest: bool = False
    kb_upload: bool = False

    @model_validator(mode="after")
    def _zero_sku_is_none(self) -> Tier:
        if self.sku_id == 0:
            self.sku_id = None
        return self


class TopUp(BaseModel):
    sku_id: int | None = None
    name: str
    price_label: str = ""
    answers: int = Field(ge=1)

    @model_validator(mode="after")
    def _zero_sku_is_none(self) -> TopUp:
        if self.sku_id == 0:
            self.sku_id = None
        return self


class ModelPrice(BaseModel):
    input: float  # USD per million input tokens
    output: float  # USD per million output tokens


class TierConfig(BaseModel):
    tier: list[Tier] = Field(min_length=1)
    topup: list[TopUp] = Field(default_factory=list)
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> TierConfig:
        if self.tier[0].sku_id is not None:
            raise ValueError("the first [[tier]] is the free tier and must not have a sku_id")
        keys = [t.key for t in self.tier]
        if len(set(keys)) != len(keys):
            raise ValueError("tier keys must be unique")
        skus = [t.sku_id for t in self.tier if t.sku_id] + [t.sku_id for t in self.topup if t.sku_id]
        if len(set(skus)) != len(skus):
            raise ValueError("sku_ids must be unique across tiers and top-ups")
        return self

    @property
    def free(self) -> Tier:
        return self.tier[0]

    @property
    def monetized(self) -> bool:
        """False when no paid tier has a SKU yet: the bot is self-hosted and
        every server gets the top tier with no answer limit."""
        return any(t.sku_id for t in self.tier[1:])

    @property
    def self_hosted(self) -> Tier:
        top = self.tier[-1]
        return top.model_copy(update={"key": "self-hosted", "name": "Self-hosted", "monthly_answers": None})

    def tier_by_sku(self, sku_id: int) -> Tier | None:
        return next((t for t in self.tier if t.sku_id == sku_id), None)

    def topup_by_sku(self, sku_id: int) -> TopUp | None:
        return next((t for t in self.topup if t.sku_id == sku_id), None)

    def rank(self, tier: Tier) -> int:
        # The synthetic self-hosted tier ranks above everything.
        return next((i for i, t in enumerate(self.tier) if t.key == tier.key), len(self.tier))

    def cheapest_with(self, feature: str) -> Tier | None:
        """The lowest tier that has a boolean feature ("digest", "kb_upload")."""
        return next((t for t in self.tier if getattr(t, feature)), None)

    def purchasable_tiers(self) -> list[Tier]:
        return [t for t in self.tier if t.sku_id]

    def purchasable_topups(self) -> list[TopUp]:
        return [t for t in self.topup if t.sku_id]

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self.prices.get(model)
        if price is None:
            return 0.0
        return (input_tokens * price.input + output_tokens * price.output) / 1_000_000


def load_tiers(path: Path) -> TierConfig:
    with path.open("rb") as f:
        return TierConfig.model_validate(tomllib.load(f))
