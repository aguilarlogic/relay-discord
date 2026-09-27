"""/relay: server setup, help channels, plan status, and stats (Manage Server)."""

from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from relay.bot import RelayBot
from relay.db import utcnow
from relay.digest import compute_stats, render_stats
from relay.plans import month_key

HelpChannel = discord.TextChannel | discord.ForumChannel

REQUIRED_PERMS = {
    "view_channel": "View Channel",
    "read_message_history": "Read Message History",
    "send_messages": "Send Messages",
    "send_messages_in_threads": "Send Messages in Threads",
    "create_public_threads": "Create Public Threads",
}


def missing_permissions(channel: HelpChannel, me: discord.Member) -> list[str]:
    perms = channel.permissions_for(me)
    return [label for attr, label in REQUIRED_PERMS.items() if not getattr(perms, attr)]


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class AdminCog(commands.GroupCog, group_name="relay", group_description="Configure Relay for this server"):
    def __init__(self, bot: RelayBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="setup", description="Set the staff role and where the daily digest goes.")
    @app_commands.describe(
        staff_role="Pinged when someone presses 'Need a human'; its members are never auto-answered",
        digest_channel="Private staff channel for the daily digest (Pro)",
        digest_hour_utc="Hour of day (UTC) to post the digest",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        staff_role: discord.Role,
        digest_channel: discord.TextChannel | None = None,
        digest_hour_utc: app_commands.Range[int, 0, 23] = 14,
    ) -> None:
        await self.bot.db.update_guild_config(
            interaction.guild_id,
            staff_role_id=staff_role.id,
            digest_channel_id=digest_channel.id if digest_channel else None,
            digest_hour_utc=digest_hour_utc,
        )
        lines = [f"Staff role: {staff_role.mention}"]
        if digest_channel:
            lines.append(f"Daily digest: {digest_channel.mention} at {digest_hour_utc:02d}:00 UTC")
        if not self.bot.help_channel_ids & {c.id for c in interaction.guild.channels}:
            lines.append("Next: add a help channel with `/relay channel-add`, then add docs with `/kb add`.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="channel-add", description="Auto-answer new questions in this channel or forum.")
    async def channel_add(self, interaction: discord.Interaction, channel: HelpChannel) -> None:
        guild_id = interaction.guild_id
        current = await self.bot.db.help_channel_ids(guild_id)
        limit = self.bot.plans.plan_for(guild_id).max_help_channels
        if channel.id not in current and limit is not None and len(current) >= limit:
            await interaction.response.send_message(
                f"The Free plan covers {limit} help channel. Remove one with `/relay channel-remove` "
                "or upgrade to Pro for unlimited channels.",
                view=self.bot.plans.upsell_view() or discord.utils.MISSING,
                ephemeral=True,
            )
            return
        missing = missing_permissions(channel, interaction.guild.me)
        await self.bot.db.add_help_channel(guild_id, channel.id)
        self.bot.help_channel_ids.add(channel.id)
        msg = f"Relay will now answer new questions in {channel.mention}."
        if missing:
            msg += f"\n⚠️ I'm missing permissions there: {', '.join(missing)}."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="channel-remove", description="Stop auto-answering in a channel or forum.")
    async def channel_remove(self, interaction: discord.Interaction, channel: HelpChannel) -> None:
        removed = await self.bot.db.remove_help_channel(interaction.guild_id, channel.id)
        self.bot.help_channel_ids.discard(channel.id)
        await interaction.response.send_message(
            f"Stopped answering in {channel.mention}." if removed else f"{channel.mention} wasn't a help channel.",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show Relay's configuration and plan usage.")
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        cfg = await self.bot.db.get_guild_config(guild.id)
        plan = self.bot.plans.plan_for(guild.id)
        now = utcnow()
        used = await self.bot.db.usage_for(guild.id, month_key(now))
        channels = await self.bot.db.help_channel_ids(guild.id)
        docs = await self.bot.kb.list_docs(guild.id)
        quota = f"{used}/{plan.monthly_answers}" if plan.monthly_answers is not None else f"{used} (unlimited)"
        lines = [
            f"**Plan:** {plan.name}",
            f"**Answers this month:** {quota}",
            "**Help channels:** " + (", ".join(f"<#{c}>" for c in sorted(channels)) or "none"),
            f"**Knowledge base:** {len(docs)} doc(s)",
            "**Staff role:** " + (f"<@&{cfg.staff_role_id}>" if cfg.staff_role_id else "not set"),
            "**Digest:** "
            + (
                f"<#{cfg.digest_channel_id}> at {cfg.digest_hour_utc:02d}:00 UTC"
                + ("" if plan.digest else " (Pro only, inactive)")
                if cfg.digest_channel_id
                else "not set"
            ),
        ]
        view = self.bot.plans.upsell_view() if plan.monthly_answers is not None else None
        await interaction.response.send_message("\n".join(lines), view=view or discord.utils.MISSING, ephemeral=True)

    @app_commands.command(name="stats", description="Questions answered, deflection rate, and time saved.")
    async def stats(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 30) -> None:
        counts = await self.bot.db.status_counts(interaction.guild_id, utcnow() - timedelta(days=days))
        await interaction.response.send_message(render_stats(compute_stats(counts), days), ephemeral=True)


async def setup(bot: RelayBot) -> None:
    await bot.add_cog(AdminCog(bot))
