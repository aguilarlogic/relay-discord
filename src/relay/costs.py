"""`relay costs`: what each tier actually costs you in AI spend, from the
per-call token log, next to what it earns.

Revenue is an estimate: active guilds on the tier x its monthly_price_usd,
before Discord's revenue share. Use it to tune tiers.toml, not for accounting.
"""

from __future__ import annotations

from dataclasses import dataclass

from relay.tiers import TierConfig


@dataclass(frozen=True)
class TierCost:
    tier: str
    calls: int
    guilds: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    price_usd: float  # monthly price of the tier (0 for free / unknown)
    days: int

    @property
    def cost_per_call(self) -> float:
        return self.cost_usd / self.calls if self.calls else 0.0

    @property
    def monthly_cost_per_guild(self) -> float:
        if not self.guilds:
            return 0.0
        return self.cost_usd / self.guilds * 30 / self.days

    @property
    def monthly_margin_per_guild(self) -> float:
        return self.price_usd - self.monthly_cost_per_guild


def summarize(rows: list[dict], tiers: TierConfig, days: int) -> list[TierCost]:
    by_tier: dict[str, dict] = {}
    for r in rows:
        agg = by_tier.setdefault(
            r["tier"], {"calls": 0, "guilds": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        agg["calls"] += r["calls"]
        # A guild's calls can span answer + digest models; take the max as the guild count.
        agg["guilds"] = max(agg["guilds"], r["guilds"])
        agg["input_tokens"] += r["input_tokens"] or 0
        agg["output_tokens"] += r["output_tokens"] or 0
        agg["cost_usd"] += tiers.estimate_cost(r["llm"], r["input_tokens"] or 0, r["output_tokens"] or 0)
    prices = {t.key: t.monthly_price_usd for t in tiers.tier}
    return [TierCost(tier=k, price_usd=prices.get(k, 0.0), days=days, **v) for k, v in by_tier.items()]


def render_report(costs: list[TierCost], days: int) -> str:
    if not costs:
        return f"No AI calls logged in the last {days} days."
    header = (
        f"{'tier':<12}{'calls':>8}{'guilds':>8}{'tokens in/out':>20}{'AI cost':>10}"
        f"{'$/call':>9}{'$/guild/mo':>12}{'price':>8}{'margin/guild':>14}"
    )
    lines = [f"AI spend over the last {days} days (estimates from tiers.toml [prices])", header, "-" * len(header)]
    for c in costs:
        margin = f"{c.monthly_margin_per_guild:>14.2f}" if c.price_usd else f"{'-':>14}"
        lines.append(
            f"{c.tier:<12}{c.calls:>8}{c.guilds:>8}{f'{c.input_tokens:,}/{c.output_tokens:,}':>20}"
            f"{c.cost_usd:>10.2f}{c.cost_per_call:>9.4f}{c.monthly_cost_per_guild:>12.2f}"
            f"{c.price_usd:>8.2f}{margin}"
        )
    total = sum(c.cost_usd for c in costs)
    lines.append(f"\nTotal AI cost: ${total:.2f}. Margins are before Discord's revenue share.")
    return "\n".join(lines)
