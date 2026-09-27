"""Persistent "Solved" / "Need a human" buttons under every answer.

DynamicItem encodes the question id in the custom_id, so the buttons keep
working after a restart without keeping any view state in memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from relay.db import ESCALATED, SOLVED, utcnow
from relay.llm import LLMError

if TYPE_CHECKING:
    from relay.bot import RelayBot

ACTIONS = {
    "solved": (SOLVED, "Solved", "✅", discord.ButtonStyle.success),
    "human": (ESCALATED, "Need a human", "🙋", discord.ButtonStyle.secondary),
}


class QuestionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"relay:q:(?P<action>solved|human):(?P<qid>[0-9]+)",
):
    def __init__(self, action: str, question_id: int) -> None:
        _, label, emoji, style = ACTIONS[action]
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"relay:q:{action}:{question_id}")
        )
        self.action = action
        self.question_id = question_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> QuestionButton:
        return cls(match["action"], int(match["qid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_question_button(interaction, self.action, self.question_id)


def answer_view(question_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(QuestionButton("solved", question_id))
    view.add_item(QuestionButton("human", question_id))
    return view


def is_staff(member: discord.abc.User, staff_role_id: int | None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    return staff_role_id is not None and any(r.id == staff_role_id for r in member.roles)


async def handle_question_button(interaction: discord.Interaction, action: str, question_id: int) -> None:
    bot: RelayBot = interaction.client  # type: ignore[assignment]
    question = await bot.db.get_question(question_id)
    if question is None or question.guild_id != interaction.guild_id:
        await interaction.response.send_message("This question is no longer tracked.", ephemeral=True)
        return

    cfg = await bot.db.get_guild_config(question.guild_id)
    if interaction.user.id != question.user_id and not is_staff(interaction.user, cfg.staff_role_id):
        await interaction.response.send_message(
            "Only the person who asked (or staff) can use these buttons.", ephemeral=True
        )
        return

    status = ACTIONS[action][0]
    if not await bot.db.resolve_question(question_id, status):
        await interaction.response.send_message("This question was already resolved.", ephemeral=True)
        return

    content = interaction.message.content if interaction.message else ""
    if status == SOLVED:
        note = f"✅ Marked solved by {interaction.user.mention}"
    else:
        note = f"🙋 {interaction.user.mention} asked for a human"
    await interaction.response.edit_message(
        content=f"{content}\n{note}"[:2000], view=None, allowed_mentions=discord.AllowedMentions.none()
    )

    if status == ESCALATED and interaction.channel is not None:
        role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        if role is None:
            text = (
                f"{interaction.user.mention} needs a human. (No staff role is configured: "
                "an admin can set one with `/relay setup`.)"
            )
            mentions = discord.AllowedMentions.none()
        else:
            text = f"{role.mention}: {interaction.user.mention} needs a human for this question."
            mentions = discord.AllowedMentions(roles=[role], users=False, everyone=False)
        await interaction.channel.send(text, allowed_mentions=mentions)


class LearnButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"relay:learn:(?P<qid>[0-9]+):(?P<mid>[0-9]+)",
):
    """Staff-only: turn a staff reply into a knowledge-base doc."""

    def __init__(self, question_id: int, message_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Save as answer",
                emoji="📚",
                style=discord.ButtonStyle.primary,
                custom_id=f"relay:learn:{question_id}:{message_id}",
            )
        )
        self.question_id = question_id
        self.message_id = message_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /) -> LearnButton:
        return cls(int(match["qid"]), int(match["mid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_learn_button(interaction, self.question_id, self.message_id)


def learn_view(question_id: int, message_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LearnButton(question_id, message_id))
    return view


async def handle_learn_button(interaction: discord.Interaction, question_id: int, message_id: int) -> None:
    bot: RelayBot = interaction.client  # type: ignore[assignment]
    question = await bot.db.get_question(question_id)
    if question is None or question.guild_id != interaction.guild_id:
        await interaction.response.send_message("This question is no longer tracked.", ephemeral=True)
        return
    cfg = await bot.db.get_guild_config(question.guild_id)
    if not is_staff(interaction.user, cfg.staff_role_id):
        await interaction.response.send_message("Only staff can save answers.", ephemeral=True)
        return
    try:
        reply = await interaction.channel.fetch_message(message_id)
    except discord.HTTPException:
        await interaction.response.send_message("That reply was deleted, so there's nothing to save.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        doc = await bot.support.learn(
            guild_id=question.guild_id,
            question=question.text,
            staff_reply=reply.content,
            source=f"learned:{question.id}",
            url=reply.jump_url,
            now=utcnow(),
        )
    except LLMError:
        await interaction.followup.send("Couldn't reach the AI to save this. Try again in a minute.", ephemeral=True)
        return
    if doc is None:
        await interaction.followup.send(
            "That reply doesn't look like a reusable answer, so nothing was saved. You can still add it with "
            "`/kb add`.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(f"Saved **{doc.title}**. Relay will answer this next time.", ephemeral=True)
    if interaction.message is not None:
        try:
            await interaction.message.edit(
                content=f"📚 {interaction.user.mention} saved this answer as **{doc.title}**.",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=60,
            )
        except discord.HTTPException:
            pass
