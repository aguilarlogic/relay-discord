"""Which tier each server is on, top-up redemption, and monthly metering.

A server's tier comes from its active Discord Premium App guild-subscription
entitlements (the highest-ranked tier wins); with none it is on the free tier.
Top-ups are consumable SKUs: each purchase adds answer credits to a server
exactly once, then the entitlement is consumed so it can be bought again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import discord

from relay.db import Database
from relay.tiers import Tier, TierConfig

logger = logging.getLogger(__name__)


def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def entitlement_is_active(ent: discord.Entitlement, now: datetime) -> bool:
    if ent.deleted:
        return False
    if ent.ends_at is not None and ent.ends_at <= now:
        return False
    return True


@dataclass(frozen=True)
class Allowance:
    monthly_limit: int | None  # None = unlimited
    used: int
    credits: int

    @property
    def remaining(self) -> int | None:
        if self.monthly_limit is None:
            return None
        return max(0, self.monthly_limit - self.used) + self.credits


class PlanService:
    def __init__(self, db: Database, tiers: TierConfig) -> None:
        self.db = db
        self.tiers = tiers
        # guild_id -> {entitlement_id: sku_id} for active tier subscriptions
        self._subs: dict[int, dict[int, int]] = {}

    @property
    def monetized(self) -> bool:
        return self.tiers.monetized

    def tier_for(self, guild_id: int) -> Tier:
        if not self.monetized:
            return self.tiers.self_hosted
        best = self.tiers.free
        for sku_id in self._subs.get(guild_id, {}).values():
            tier = self.tiers.tier_by_sku(sku_id)
            if tier is not None and self.tiers.rank(tier) > self.tiers.rank(best):
                best = tier
        return best

    # --- subscriptions ------------------------------------------------------

    def apply_entitlement(self, ent: discord.Entitlement, now: datetime) -> None:
        """Track a subscription entitlement from a gateway event or an
        interaction. Top-up entitlements are ignored here (see redeem_topup)."""
        if ent.guild_id is None or self.tiers.tier_by_sku(ent.sku_id) is None:
            return
        subs = self._subs.setdefault(ent.guild_id, {})
        if entitlement_is_active(ent, now):
            subs[ent.id] = ent.sku_id
        else:
            subs.pop(ent.id, None)

    def note_interaction_entitlements(self, interaction: discord.Interaction, now: datetime) -> None:
        """Interactions carry the guild's current entitlements -- a free,
        always-fresh signal, so a new subscriber is upgraded on their next click."""
        for ent in interaction.entitlements:
            self.apply_entitlement(ent, now)

    async def refresh(self, client: discord.Client, now: datetime) -> None:
        tier_skus = [discord.Object(id=t.sku_id) for t in self.tiers.purchasable_tiers()]
        if tier_skus:
            subs: dict[int, dict[int, int]] = {}
            async for ent in client.entitlements(skus=tier_skus, exclude_ended=True, limit=None):
                if ent.guild_id is not None and entitlement_is_active(ent, now):
                    subs.setdefault(ent.guild_id, {})[ent.id] = ent.sku_id
            self._subs = subs
            logger.info("entitlements refreshed: %d subscribed guild(s)", len(subs))

        # Catch up on guild-owned top-ups whose create event we missed.
        topup_skus = [discord.Object(id=t.sku_id) for t in self.tiers.purchasable_topups()]
        if topup_skus:
            async for ent in client.entitlements(skus=topup_skus, limit=None):
                if ent.guild_id is not None and not ent.consumed:
                    await self.redeem_topup(ent, ent.guild_id, now)

    # --- top-ups ------------------------------------------------------------

    async def redeem_topup(self, ent: discord.Entitlement, guild_id: int, now: datetime) -> int | None:
        """Credit a top-up purchase to guild_id. Returns the answers added, or
        None if this isn't a top-up or it was already redeemed."""
        topup = self.tiers.topup_by_sku(ent.sku_id)
        if topup is None:
            return None
        added = await self.db.redeem_entitlement(ent.id, guild_id, topup.answers, now)
        if not ent.consumed:
            try:
                await ent.consume()
            except discord.HTTPException:
                # Already credited; the next refresh will retry consuming it.
                logger.warning("could not consume top-up entitlement %s", ent.id)
        if added:
            logger.info("guild %s redeemed %s (+%d answers)", guild_id, topup.name, topup.answers)
        return topup.answers if added else None

    # --- metering -----------------------------------------------------------

    async def allowance(self, guild_id: int, now: datetime) -> Allowance:
        return Allowance(
            monthly_limit=self.tier_for(guild_id).monthly_answers,
            used=await self.db.usage_for(guild_id, month_key(now)),
            credits=await self.db.credit_balance(guild_id),
        )

    async def consume(self, guild_id: int, now: datetime) -> None:
        """Charge one AI call: the monthly allowance first, then top-up credits."""
        allowance = await self.allowance(guild_id, now)
        if (
            allowance.monthly_limit is not None
            and allowance.used >= allowance.monthly_limit
            and await self.db.spend_credit(guild_id)
        ):
            return
        await self.db.increment_usage(guild_id, month_key(now))

    # --- upsell -------------------------------------------------------------

    def upsell_view(self, guild_id: int, *, include_topups: bool = True) -> discord.ui.View | None:
        """Premium buttons for tiers above the server's current one (and the
        top-up packs). Discord renders each button with the SKU's name and price."""
        if not self.monetized:
            return None
        current_rank = self.tiers.rank(self.tier_for(guild_id))
        skus = [t.sku_id for t in self.tiers.purchasable_tiers() if self.tiers.rank(t) > current_rank]
        if include_topups:
            skus += [t.sku_id for t in self.tiers.purchasable_topups()]
        if not skus:
            return None
        view = discord.ui.View(timeout=None)
        for sku_id in skus[:25]:
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.premium, sku_id=sku_id))
        return view
