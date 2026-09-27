"""Scheduled work: the daily staff digest, retention purge, entitlement refresh."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from relay.bot import RelayBot
from relay.db import DECLINED, utcnow
from relay.digest import build_digest, cluster_gaps, render_digest

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30


async def make_digest(bot: RelayBot, guild_id: int, now: datetime) -> str:
    questions = await bot.db.questions_since(guild_id, now - timedelta(days=1))
    digest = build_digest(questions)
    declined = [q for q in questions if q.status == DECLINED]
    if declined:
        digest.gaps = await cluster_gaps(bot.fast_llm, declined)
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
                or not self.bot.plans.plan_for(cfg.guild_id).digest
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
        try:
            await self.bot.plans.refresh(self.bot, now)
        except discord.HTTPException:
            logger.exception("entitlement refresh failed")

    @digest_loop.before_loop
    @maintenance_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="digest-now", description="Preview today's staff digest (Pro).")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def digest_now(self, interaction: discord.Interaction) -> None:
        if not self.bot.plans.plan_for(interaction.guild_id).digest:
            await interaction.response.send_message(
                "The daily digest is a Pro feature.",
                view=self.bot.plans.upsell_view() or discord.utils.MISSING,
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await make_digest(self.bot, interaction.guild_id, utcnow()), ephemeral=True)


async def setup(bot: RelayBot) -> None:
    await bot.add_cog(DigestCog(bot))
