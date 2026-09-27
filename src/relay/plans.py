"""Free vs Pro plans, Discord Premium App entitlements, and monthly metering.

Pro status comes from Discord's guild-subscription entitlements for the
configured SKU. With no SKU configured the bot runs self-hosted: every guild
gets the unlimited plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import discord

from relay.db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_answers: int | None  # None = unlimited
    max_help_channels: int | None
    digest: bool
    kb_upload: bool


FREE = Plan("Free", monthly_answers=50, max_help_channels=1, digest=False, kb_upload=False)
PRO = Plan("Pro", monthly_answers=None, max_help_channels=None, digest=True, kb_upload=True)
SELF_HOSTED = Plan("Self-hosted", monthly_answers=None, max_help_channels=None, digest=True, kb_upload=True)


def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def entitlement_is_active(ent: discord.Entitlement, now: datetime) -> bool:
    if ent.deleted:
        return False
    if ent.ends_at is not None and ent.ends_at <= now:
        return False
    return True


class PlanService:
    def __init__(self, db: Database, sku_id: int | None) -> None:
        self.db = db
        self.sku_id = sku_id
        self._pro_guilds: set[int] = set()

    @property
    def monetized(self) -> bool:
        return self.sku_id is not None

    def plan_for(self, guild_id: int) -> Plan:
        if not self.monetized:
            return SELF_HOSTED
        return PRO if guild_id in self._pro_guilds else FREE

    def apply_entitlement(self, ent: discord.Entitlement, now: datetime) -> None:
        """Handle an entitlement create/update/delete gateway event."""
        if not self.monetized or ent.sku_id != self.sku_id or ent.guild_id is None:
            return
        if entitlement_is_active(ent, now):
            self._pro_guilds.add(ent.guild_id)
        else:
            self._pro_guilds.discard(ent.guild_id)

    def note_interaction_entitlements(self, interaction: discord.Interaction, now: datetime) -> None:
        """Interactions carry the guild's current entitlements -- a free,
        always-fresh signal, so a new subscriber gets Pro on their next click."""
        for ent in interaction.entitlements:
            self.apply_entitlement(ent, now)

    async def refresh(self, client: discord.Client, now: datetime) -> None:
        if not self.monetized:
            return
        pro: set[int] = set()
        async for ent in client.entitlements(skus=[discord.Object(id=self.sku_id)], exclude_ended=True, limit=None):
            if ent.guild_id is not None and entitlement_is_active(ent, now):
                pro.add(ent.guild_id)
        self._pro_guilds = pro
        logger.info("entitlements refreshed: %d pro guild(s)", len(pro))

    async def remaining_answers(self, guild_id: int, now: datetime) -> int | None:
        """None = unlimited."""
        limit = self.plan_for(guild_id).monthly_answers
        if limit is None:
            return None
        return max(0, limit - await self.db.usage_for(guild_id, month_key(now)))

    async def record_answer(self, guild_id: int, now: datetime) -> None:
        await self.db.increment_usage(guild_id, month_key(now))

    def upsell_view(self) -> discord.ui.View | None:
        if not self.monetized:
            return None
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.premium, sku_id=self.sku_id))
        return view
