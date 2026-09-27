"""RelayBot: wires settings, storage, and services into a discord.py client."""

from __future__ import annotations

import logging

import aiohttp
import anthropic
import discord
from discord import app_commands
from discord.ext import commands

from relay.config import Settings
from relay.crawl import make_crawl_session
from relay.db import Database, utcnow
from relay.kb import KnowledgeBase
from relay.llm import ClaudeLLM, LLMRouter, OpenAICompatLLM
from relay.plans import PlanService, month_key
from relay.support import SupportService
from relay.tiers import FREE_LLM, load_tiers
from relay.views import LearnButton, QuestionButton

logger = logging.getLogger(__name__)

COGS = ("relay.cogs.support", "relay.cogs.kb", "relay.cogs.admin", "relay.cogs.digest")


class RelayTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Every interaction carries the guild's live entitlements; use them so
        # a server that just subscribed is upgraded immediately.
        self.client.plans.note_interaction_entitlements(interaction, utcnow())  # type: ignore[attr-defined]
        return True


class RelayBot(commands.Bot):
    db: Database
    kb: KnowledgeBase
    plans: PlanService
    support: SupportService
    router: LLMRouter
    fast_llm: ClaudeLLM
    http: aiohttp.ClientSession
    crawl_http: aiohttp.ClientSession

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
        tiers = load_tiers(s.relay_tiers_path)
        self.db = await Database.open(s.database_path)
        self.http = aiohttp.ClientSession()
        self.crawl_http = make_crawl_session()  # public IPs only: crawl URLs come from users
        # api_key=None lets the SDK fall back to its own credential resolution.
        claude = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
        free_llm = None
        if s.free_llm_model:
            free_llm = OpenAICompatLLM(
                self.http,
                base_url=s.free_llm_base_url,
                api_key=s.free_llm_api_key,
                model=s.free_llm_model,
                json_mode=s.free_llm_json_mode,
            )
        elif any(t.llm == FREE_LLM for t in tiers.tier):
            logger.warning("a tier uses llm='free' but FREE_LLM_MODEL is not set: those servers get no answers")
        self.router = LLMRouter(
            claude,
            free_llm,
            fast_model=s.relay_fast_model,
            effort=s.relay_effort,
            refusal_fallback=s.relay_refusal_fallback,
        )
        self.fast_llm = ClaudeLLM(claude, s.relay_fast_model, refusal_fallback=False)
        self.kb = KnowledgeBase(self.db)
        self.plans = PlanService(self.db, tiers)
        self.support = SupportService(self.db, self.kb, self.plans, self.router)
        self.help_channel_ids = await self.db.all_help_channel_ids()

        self.add_dynamic_items(QuestionButton, LearnButton)
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
        for session in ("http", "crawl_http"):
            if hasattr(self, session):
                await getattr(self, session).close()
        if hasattr(self, "db"):
            await self.db.close()

    async def on_ready(self) -> None:
        logger.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))
        try:
            await self.plans.refresh(self, utcnow())
        except discord.HTTPException:
            logger.exception("could not load entitlements")

    async def on_entitlement_create(self, ent: discord.Entitlement) -> None:
        now = utcnow()
        self.plans.apply_entitlement(ent, now)
        if ent.guild_id is not None:
            # Top-ups bought for a server are credited right away; ones owned by
            # a user are redeemed into a server with /relay redeem.
            await self.plans.redeem_topup(ent, ent.guild_id, now)

    async def on_entitlement_update(self, ent: discord.Entitlement) -> None:
        self.plans.apply_entitlement(ent, utcnow())

    async def on_entitlement_delete(self, ent: discord.Entitlement) -> None:
        self.plans.apply_entitlement(ent, utcnow())

    async def notify_limit_reached(self, guild: discord.Guild) -> None:
        """Tell staff (once per month) that the server is out of answers.
        Members are never shown upsells."""
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
        tier = self.plans.tier_for(guild.id)
        text = (
            f"**Relay has used this server's {tier.monthly_answers} {tier.name} answers for the month.** "
            "New questions will wait for staff until the allowance resets on the 1st. Upgrade to a higher "
            "plan, or buy a one-time answer pack (then run `/relay redeem`). See `/relay plans` for options."
        )
        view = self.plans.upsell_view(guild.id)
        try:
            await channel.send(text, view=view or discord.utils.MISSING)
        except discord.HTTPException:
            logger.warning("could not post limit notice in guild %s", guild.id)
