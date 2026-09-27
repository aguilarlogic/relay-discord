"""RelayBot: wires settings, storage, and services into a discord.py client."""

from __future__ import annotations

import logging

import anthropic
import discord
from discord import app_commands
from discord.ext import commands

from relay.answer import Answerer
from relay.config import Settings
from relay.db import Database, utcnow
from relay.kb import KnowledgeBase
from relay.llm import StructuredLLM
from relay.plans import PlanService, month_key
from relay.support import SupportService
from relay.views import QuestionButton

logger = logging.getLogger(__name__)

COGS = ("relay.cogs.support", "relay.cogs.kb", "relay.cogs.admin", "relay.cogs.digest")


class RelayTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Every interaction carries the guild's live entitlements; use them so
        # a server that just subscribed gets Pro immediately.
        self.client.plans.note_interaction_entitlements(interaction, utcnow())  # type: ignore[attr-defined]
        return True


class RelayBot(commands.Bot):
    db: Database
    kb: KnowledgeBase
    plans: PlanService
    support: SupportService
    fast_llm: StructuredLLM

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Developer Portal
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=RelayTree,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.settings = settings
        self.help_channel_ids: set[int] = set()

    async def setup_hook(self) -> None:
        s = self.settings
        self.db = await Database.open(s.database_path)
        # api_key=None lets the SDK fall back to its own credential resolution.
        client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
        answer_llm = StructuredLLM(
            client, s.relay_model, effort=s.relay_effort, refusal_fallback=s.relay_refusal_fallback
        )
        self.fast_llm = StructuredLLM(client, s.relay_fast_model, refusal_fallback=False)
        self.kb = KnowledgeBase(self.db)
        self.plans = PlanService(self.db, s.premium_sku_id)
        self.support = SupportService(self.db, self.kb, Answerer(answer_llm), self.plans)
        self.help_channel_ids = await self.db.all_help_channel_ids()

        self.add_dynamic_items(QuestionButton)
        for cog in COGS:
            await self.load_extension(cog)

        if s.dev_guild_id:
            guild = discord.Object(id=s.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("synced %d commands to dev guild %s", len(synced), s.dev_guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("synced %d global commands (may take a while to appear)", len(synced))

    async def close(self) -> None:
        await super().close()
        if hasattr(self, "db"):
            await self.db.close()

    async def on_ready(self) -> None:
        logger.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))
        try:
            await self.plans.refresh(self, utcnow())
        except discord.HTTPException:
            logger.exception("could not load entitlements")

    async def on_entitlement_create(self, ent: discord.Entitlement) -> None:
        self.plans.apply_entitlement(ent, utcnow())

    async def on_entitlement_update(self, ent: discord.Entitlement) -> None:
        self.plans.apply_entitlement(ent, utcnow())

    async def on_entitlement_delete(self, ent: discord.Entitlement) -> None:
        self.plans.apply_entitlement(ent, utcnow())

    async def notify_limit_reached(self, guild: discord.Guild) -> None:
        """Tell staff (once per month) that the free tier ran out. Members are
        never shown upsells."""
        now = utcnow()
        month = month_key(now)
        cfg = await self.db.get_guild_config(guild.id)
        if cfg.last_limit_notice_month == month:
            return
        await self.db.update_guild_config(guild.id, last_limit_notice_month=month)
        channel = guild.get_channel(cfg.digest_channel_id) if cfg.digest_channel_id else None
        channel = channel or guild.system_channel
        if not isinstance(channel, discord.TextChannel):
            return
        text = (
            "**Relay has used this server's free answers for the month.** New questions will wait for "
            "staff until the limit resets on the 1st. Upgrade to Pro for unlimited answers, more help "
            "channels, file imports, and the daily staff digest."
        )
        view = self.plans.upsell_view()
        try:
            await channel.send(text, view=view) if view else await channel.send(text)
        except discord.HTTPException:
            logger.warning("could not post limit notice in guild %s", guild.id)
