"""Auto-answers in help channels / forums, plus /ask."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from relay.bot import RelayBot
from relay.db import utcnow
from relay.support import HandleResult, Outcome, looks_like_question
from relay.views import answer_view, is_staff

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()


def format_answer(result: HandleResult) -> str:
    assert result.answer is not None
    lines = [result.answer.text, ""]
    if result.source_titles:
        lines.append("-# 📚 Sources: " + " · ".join(result.source_titles))
    lines.append("-# 🤖 AI answer from this server's docs. Not right? Press **Need a human**.")
    return "\n".join(lines)[:2000]


def thread_name(text: str) -> str:
    name = " ".join(text.split())
    return (name[:90] + "…") if len(name) > 90 else (name or "Question")


class SupportCog(commands.Cog):
    def __init__(self, bot: RelayBot) -> None:
        self.bot = bot

    def _question_target(self, message: discord.Message) -> str | None:
        """'forum' for a new forum post, 'channel' for a top-level message in
        a help text channel, None for anything Relay should ignore (including
        follow-up conversation inside threads)."""
        channel = message.channel
        if isinstance(channel, discord.Thread):
            # A forum post's starter message has the same id as its thread.
            if channel.parent_id in self.bot.help_channel_ids and message.id == channel.id:
                return "forum"
            return None
        if channel.id in self.bot.help_channel_ids and message.type is discord.MessageType.default:
            return "channel"
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        target = self._question_target(message)
        if target is None:
            return

        text = message.content
        if target == "forum":
            text = f"{message.channel.name}\n{text}"  # the post title is part of the question
        if not looks_like_question(text):
            return
        cfg = await self.bot.db.get_guild_config(message.guild.id)
        if is_staff(message.author, cfg.staff_role_id):
            return

        result = await self.bot.support.handle_question(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            user_id=message.author.id,
            text=text,
            now=utcnow(),
        )
        if result.outcome is Outcome.LIMITED:
            await self.bot.notify_limit_reached(message.guild)
            return
        if result.outcome is not Outcome.ANSWERED:
            return

        try:
            if target == "forum":
                reply_to: discord.abc.Messageable = message.channel
            else:
                reply_to = await message.create_thread(name=thread_name(message.content))
            await reply_to.send(
                format_answer(result), view=answer_view(result.question_id), allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException:
            logger.exception("could not post answer in channel %s", message.channel.id)

    @app_commands.command(name="ask", description="Ask a question; Relay answers from this server's docs.")
    @app_commands.guild_only()
    @app_commands.describe(question="What do you need help with?")
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 5, 1000]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.support.handle_question(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=None,
            user_id=interaction.user.id,
            text=question,
            now=utcnow(),
        )
        if result.outcome is Outcome.ANSWERED:
            await interaction.followup.send(
                f"> {question}\n\n" + format_answer(result)[: 1990 - len(question)],
                view=answer_view(result.question_id),
                ephemeral=True,
            )
            return
        messages = {
            Outcome.COOLDOWN: "You're asking a little fast. Try again in a minute.",
            Outcome.LIMITED: "Relay is out of answers for this month. Please ask in a help channel.",
            Outcome.DECLINED: "I couldn't find that in this server's docs. Please ask in a help channel "
            "so staff can help (and add it to the docs).",
            Outcome.ERROR: "Something went wrong while answering. Please try again later.",
        }
        await interaction.followup.send(messages[result.outcome], ephemeral=True)
        if result.outcome is Outcome.LIMITED and interaction.guild is not None:
            await self.bot.notify_limit_reached(interaction.guild)


async def setup(bot: RelayBot) -> None:
    await bot.add_cog(SupportCog(bot))
