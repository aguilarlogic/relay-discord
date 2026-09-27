"""Auto-answers in help channels / forums, /ask, and learning from staff."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from relay.bot import RelayBot
from relay.db import utcnow
from relay.kb import DocRef
from relay.llm import LLMError
from relay.support import HandleResult, Outcome, looks_like_question
from relay.views import answer_view, is_staff, learn_view

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
MIN_STAFF_REPLY_CHARS = 20
LEARN_PROMPT_SECONDS = 600
# Forum tags that mean "this post is answered" (matched case-insensitively).
SOLVED_TAG_WORDS = ("solved", "resolved", "answered", "fixed")
MAX_THREAD_MESSAGES = 100


def _source_label(ref: DocRef) -> str:
    title = ref.title.replace("[", "(").replace("]", ")")
    # <...> stops Discord from unfurling a link preview under the answer.
    return f"[{title}](<{ref.url}>)" if ref.url else title


def format_answer(result: HandleResult) -> str:
    assert result.answer is not None
    lines = [result.answer.text, ""]
    if result.sources:
        lines.append("-# 📚 Sources: " + " · ".join(_source_label(r) for r in result.sources))
    lines.append("-# 🤖 AI answer from this server's docs. Not right? Press **Need a human**.")
    return "\n".join(lines)[:2000]


def thread_name(text: str) -> str:
    name = " ".join(text.split())
    return (name[:90] + "…") if len(name) > 90 else (name or "Question")


def has_solved_tag(thread: discord.Thread) -> bool:
    return any(any(w in tag.name.lower() for w in SOLVED_TAG_WORDS) for tag in thread.applied_tags)


class SupportCog(commands.Cog):
    def __init__(self, bot: RelayBot) -> None:
        self.bot = bot
        self._offered: set[int] = set()  # question ids already offered for learning
        self.save_menu = app_commands.ContextMenu(name="Save to Relay docs", callback=self.save_message_to_docs)
        self.save_menu.guild_only = True
        self.save_menu.default_permissions = discord.Permissions(manage_messages=True)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.save_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.save_menu.name, type=self.save_menu.type)

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
        channel = message.channel
        in_help_area = (
            channel.parent_id in self.bot.help_channel_ids
            if isinstance(channel, discord.Thread)
            else channel.id in self.bot.help_channel_ids
        )
        if not in_help_area:
            return  # fast path: Relay only works in help channels and their threads

        cfg = await self.bot.db.get_guild_config(message.guild.id)
        if is_staff(message.author, cfg.staff_role_id):
            await self._maybe_offer_learning(message)
            return
        if target is None:
            return

        text = message.content
        if target == "forum":
            text = f"{message.channel.name}\n{text}"  # the post title is part of the question
        if not looks_like_question(text):
            return

        result = await self.bot.support.handle_question(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            user_id=message.author.id,
            text=text,
            now=utcnow(),
            thread_id=message.channel.id if target == "forum" else None,
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
                await self.bot.db.set_question_thread(result.question_id, reply_to.id)
            await reply_to.send(
                format_answer(result), view=answer_view(result.question_id), allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException:
            logger.exception("could not post answer in channel %s", message.channel.id)

    async def _maybe_offer_learning(self, message: discord.Message) -> None:
        """A staff member just replied to a question Relay couldn't answer
        (in its thread, or as a Discord reply to it): offer to save the reply."""
        if len(message.content.strip()) < MIN_STAFF_REPLY_CHARS:
            return
        thread_id = message.channel.id if isinstance(message.channel, discord.Thread) else None
        ref_id = message.reference.message_id if message.reference else None
        # A thread started from a message shares that message's id, so a reply
        # inside it also matches the question by message id.
        question = await self.bot.db.find_unanswered_question(
            message.guild.id, thread_id=thread_id, message_id=ref_id or thread_id
        )
        if question is None or question.id in self._offered:
            return
        self._offered.add(question.id)
        if len(self._offered) > 50_000:
            self._offered.clear()
        try:
            await message.reply(
                "-# 📚 Staff: save this reply so Relay can answer this question next time?",
                view=learn_view(question.id, message.id),
                mention_author=False,
                allowed_mentions=NO_MENTIONS,
                delete_after=LEARN_PROMPT_SECONDS,
            )
        except discord.HTTPException:
            logger.warning("could not post learn prompt in channel %s", message.channel.id)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        """A forum post in a help forum just got a "Solved"-style tag: learn
        the staff answer from the thread."""
        if after.parent_id not in self.bot.help_channel_ids or has_solved_tag(before) or not has_solved_tag(after):
            return
        try:
            await self.learn_thread(after)
        except (discord.HTTPException, LLMError):
            logger.exception("could not learn from solved thread %s", after.id)

    async def learn_thread(self, thread: discord.Thread) -> str | None:
        """Save the staff answer(s) in a thread as a doc linked to the thread.
        Returns the doc title, or None if there was nothing to learn."""
        cfg = await self.bot.db.get_guild_config(thread.guild.id)
        question = thread.name
        staff_replies: list[str] = []
        async for msg in thread.history(limit=MAX_THREAD_MESSAGES, oldest_first=True):
            if msg.id == thread.id:
                question = f"{thread.name}\n{msg.content}"
            elif not msg.author.bot and is_staff(msg.author, cfg.staff_role_id) and msg.content.strip():
                staff_replies.append(msg.content.strip())
        if not staff_replies:
            return None
        doc = await self.bot.support.learn(
            guild_id=thread.guild.id,
            question=question,
            staff_reply="\n\n".join(staff_replies[-3:]),
            source=f"thread:{thread.id}",
            url=thread.jump_url,
            now=utcnow(),
        )
        if doc is not None:
            try:
                await thread.send(
                    f"-# 📚 Relay learned this answer as **{doc.title}** and will reuse it for similar questions.",
                    allowed_mentions=NO_MENTIONS,
                )
            except discord.HTTPException:
                pass
        return doc.title if doc else None

    async def save_message_to_docs(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Message context menu: Apps -> Save to Relay docs."""
        cfg = await self.bot.db.get_guild_config(interaction.guild_id)
        if not is_staff(interaction.user, cfg.staff_role_id):
            await interaction.response.send_message("Only staff can save answers.", ephemeral=True)
            return
        if len(message.content.strip()) < MIN_STAFF_REPLY_CHARS:
            await interaction.response.send_message("That message is too short to be a useful answer.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Context: the message it replies to, else the thread it's in.
        question = ""
        if message.reference and message.reference.message_id:
            ref = message.reference.resolved
            if not isinstance(ref, discord.Message):
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except discord.HTTPException:
                    ref = None
            if isinstance(ref, discord.Message):
                question = ref.content
        if not question and isinstance(message.channel, discord.Thread):
            question = message.channel.name

        try:
            doc = await self.bot.support.learn(
                guild_id=interaction.guild_id,
                question=question,
                staff_reply=message.content,
                source=f"message:{message.id}",
                url=message.jump_url,
                now=utcnow(),
            )
        except LLMError:
            await interaction.followup.send(
                "Couldn't reach the AI to save this. Try again in a minute.", ephemeral=True
            )
            return
        if doc is None:
            await interaction.followup.send(
                "That message doesn't look like a reusable answer, so nothing was saved.", ephemeral=True
            )
            return
        await interaction.followup.send(f"Saved **{doc.title}** to Relay's docs.", ephemeral=True)

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
