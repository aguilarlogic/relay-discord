"""Scheduled work: the daily staff digest, retention purge, entitlement refresh."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from relay.bot import RelayBot
from relay.cogs.kb import sync_site
from relay.crawl import CrawlError
from relay.db import DECLINED, iso, utcnow
from relay.digest import build_digest, cluster_gaps, render_digest

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
CALL_LOG_RETENTION_DAYS = 120
SITE_RESYNC_HOURS = 20


async def make_digest(bot: RelayBot, guild_id: int, now: datetime) -> str:
    questions = await bot.db.questions_since(guild_id, now - timedelta(days=1))
    digest = build_digest(questions)
    digest.learned = await bot.kb.count_new_docs(guild_id, "learned:", iso(now - timedelta(days=1)))
    digest.learned += await bot.kb.count_new_docs(guild_id, "thread:", iso(now - timedelta(days=1)))
    digest.learned += await bot.kb.count_new_docs(guild_id, "message:", iso(now - timedelta(days=1)))
    declined = [q for q in questions if q.status == DECLINED]
    if declined:
        digest.gaps, usage = await cluster_gaps(bot.fast_llm, declined)
        if usage is not None:
            await bot.db.log_llm_call(
                guild_id=guild_id,
                tier=bot.plans.tier_for(guild_id).key,
                llm=bot.fast_llm.model,
                purpose="digest",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                now=now,
            )
    return render_digest(digest, guild_id)


class DigestCog(commands.Cog):
    def __init__(self, bot: RelayBot) -> None:
        self.bot = bot
        self.digest_loop.start()
        self.maintenance_loop.start()

    async def cog_unload(self) -> None:
        self.digest_loop.cancel()
        self.maintenance_loop.cancel()

    @tasks.loop(minutes=10)
    async def digest_loop(self) -> None:
        now = utcnow()
        today = now.date().isoformat()
        for cfg in await self.bot.db.all_guild_configs():
            if (
                cfg.digest_channel_id is None
                or cfg.digest_hour_utc != now.hour
                or cfg.last_digest_date == today
                or not self.bot.plans.tier_for(cfg.guild_id).digest
            ):
                continue
            guild = self.bot.get_guild(cfg.guild_id)
            channel = guild.get_channel(cfg.digest_channel_id) if guild else None
            # Mark first: a failure should skip today, not retry every 10 minutes.
            await self.bot.db.update_guild_config(cfg.guild_id, last_digest_date=today)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(await make_digest(self.bot, cfg.guild_id, now))
            except discord.HTTPException:
                logger.exception("could not post digest in guild %s", cfg.guild_id)

    @tasks.loop(hours=6)
    async def maintenance_loop(self) -> None:
        now = utcnow()
        purged = await self.bot.db.purge_questions_before(now - timedelta(days=RETENTION_DAYS))
        if purged:
            logger.info("purged %d questions older than %d days", purged, RETENTION_DAYS)
        # Token logs hold no message content; keep them longer for cost reports.
        await self.bot.db.purge_llm_calls_before(now - timedelta(days=CALL_LOG_RETENTION_DAYS))
        try:
            await self.bot.plans.refresh(self.bot, now)
        except discord.HTTPException:
            logger.exception("entitlement refresh failed")
        await self.resync_sites(now)

    async def resync_sites(self, now: datetime) -> None:
        cutoff = iso(now - timedelta(hours=SITE_RESYNC_HOURS))
        for site in await self.bot.db.sites():
            if site["last_synced_at"] and site["last_synced_at"] > cutoff:
                continue
            if self.bot.get_guild(site["guild_id"]) is None:
                continue  # bot was removed from that server
            try:
                await sync_site(self.bot, site["guild_id"], site["url"], now)
            except CrawlError as e:
                # Keep the old pages; try again next cycle.
                logger.warning("re-sync of %s for guild %s failed: %s", site["url"], site["guild_id"], e)

    @digest_loop.before_loop
    @maintenance_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="digest-now", description="Preview today's staff digest (paid plans).")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def digest_now(self, interaction: discord.Interaction) -> None:
        plans = self.bot.plans
        if not plans.tier_for(interaction.guild_id).digest:
            needed = plans.tiers.cheapest_with("digest")
            await interaction.response.send_message(
                f"The daily digest needs the {needed.name if needed else 'a paid'} plan or higher. See `/relay plans`.",
                view=plans.upsell_view(interaction.guild_id, include_topups=False) or discord.utils.MISSING,
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await make_digest(self.bot, interaction.guild_id, utcnow()), ephemeral=True)


async def setup(bot: RelayBot) -> None:
    await bot.add_cog(DigestCog(bot))
