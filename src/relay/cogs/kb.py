"""/kb: manage the server's knowledge base (Manage Server permission)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from relay.bot import RelayBot

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 512 * 1024
UPLOAD_EXTENSIONS = (".md", ".markdown", ".txt")
MAX_PINS = 250


class AddDocModal(discord.ui.Modal, title="Add to Relay's knowledge base"):
    doc_title = discord.ui.TextInput(label="Title", placeholder="e.g. Refund policy", max_length=100)
    content = discord.ui.TextInput(
        label="Content",
        style=discord.TextStyle.paragraph,
        placeholder="Paste the FAQ answer, policy, or guide. Plain text or markdown.",
        max_length=4000,
    )

    def __init__(self, bot: RelayBot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            doc_id, n = await self.bot.kb.add_doc(
                interaction.guild_id, self.doc_title.value, self.content.value, source="manual"
            )
        except ValueError as e:
            await interaction.response.send_message(f"Couldn't add that: {e}.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added **{self.doc_title.value}** as doc `#{doc_id}` ({n} chunk{'s' * (n != 1)}).", ephemeral=True
        )


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class KBCog(commands.GroupCog, group_name="kb", group_description="Manage Relay's knowledge base"):
    def __init__(self, bot: RelayBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="add", description="Add a doc by pasting text.")
    async def add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddDocModal(self.bot))

    @app_commands.command(name="upload", description="Import a .md or .txt file (paid plans).")
    @app_commands.describe(file="A .md or .txt file", title="Title (defaults to the file name)")
    async def upload(
        self, interaction: discord.Interaction, file: discord.Attachment, title: str | None = None
    ) -> None:
        plans = self.bot.plans
        if not plans.tier_for(interaction.guild_id).kb_upload:
            needed = plans.tiers.cheapest_with("kb_upload")
            await interaction.response.send_message(
                f"File import needs the {needed.name if needed else 'a paid'} plan or higher. "
                "Use `/kb add` to paste text instead, or see `/relay plans`.",
                view=plans.upsell_view(interaction.guild_id, include_topups=False) or discord.utils.MISSING,
                ephemeral=True,
            )
            return
        if not file.filename.lower().endswith(UPLOAD_EXTENSIONS):
            await interaction.response.send_message("Upload a .md or .txt file.", ephemeral=True)
            return
        if file.size > MAX_UPLOAD_BYTES:
            await interaction.response.send_message("That file is too large (max 512 KB).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = (await file.read()).decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send("That file isn't valid UTF-8 text.", ephemeral=True)
            return
        doc_title = title or file.filename.rsplit(".", 1)[0]
        try:
            doc_id, n = await self.bot.kb.add_doc(interaction.guild_id, doc_title, text, source=f"file:{file.filename}")
        except ValueError as e:
            await interaction.followup.send(f"Couldn't import: {e}.", ephemeral=True)
            return
        await interaction.followup.send(f"Imported **{doc_title}** as doc `#{doc_id}` ({n} chunks).", ephemeral=True)

    @app_commands.command(name="import-pins", description="Import a channel's pinned messages as one doc.")
    async def import_pins(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        parts: list[str] = []
        try:
            async for msg in channel.pins(limit=MAX_PINS):
                if msg.content.strip():
                    parts.append(msg.content.strip())
        except discord.Forbidden:
            await interaction.followup.send(
                f"I can't read pins in {channel.mention} (I need Read Message History there).", ephemeral=True
            )
            return
        if not parts:
            await interaction.followup.send(f"No text pins found in {channel.mention}.", ephemeral=True)
            return
        doc_title = f"Pinned messages in #{channel.name}"
        doc_id, n = await self.bot.kb.add_doc(
            interaction.guild_id, doc_title, "\n\n".join(parts), source=f"pins:{channel.id}"
        )
        await interaction.followup.send(f"Imported {len(parts)} pins as doc `#{doc_id}` ({n} chunks).", ephemeral=True)

    @app_commands.command(name="list", description="List the docs Relay answers from.")
    async def list_docs(self, interaction: discord.Interaction) -> None:
        docs = await self.bot.kb.list_docs(interaction.guild_id)
        if not docs:
            await interaction.response.send_message(
                "The knowledge base is empty. Add docs with `/kb add`, `/kb upload`, or `/kb import-pins`.",
                ephemeral=True,
            )
            return
        lines = [f"`#{d.id}` **{d.title}** · {d.char_count:,} chars · {d.source}" for d in docs]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900].rsplit("\n", 1)[0] + f"\n…and more ({len(docs)} docs total)"
        await interaction.response.send_message(body, ephemeral=True)

    @app_commands.command(name="remove", description="Remove a doc by its id (see /kb list).")
    async def remove(self, interaction: discord.Interaction, doc_id: int) -> None:
        removed = await self.bot.kb.remove_doc(interaction.guild_id, doc_id)
        await interaction.response.send_message(
            f"Removed doc `#{doc_id}`." if removed else f"No doc `#{doc_id}` in this server.", ephemeral=True
        )


async def setup(bot: RelayBot) -> None:
    await bot.add_cog(KBCog(bot))
