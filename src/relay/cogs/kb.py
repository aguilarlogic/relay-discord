"""/kb: manage the server's knowledge base (Manage Server permission)."""

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from relay.bot import RelayBot
from relay.crawl import Crawler, CrawlError, normalize_url
from relay.db import utcnow

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 512 * 1024
UPLOAD_EXTENSIONS = (".md", ".markdown", ".txt")
MAX_PINS = 250


def site_source(url: str) -> str:
    return f"site:{url}"


async def site_page_budget(bot: RelayBot, guild_id: int, url: str) -> int:
    """Pages this site may use: the tier's kb_pages minus other synced sites."""
    others = sum(s["pages"] for s in await bot.db.sites(guild_id) if s["url"] != url)
    return max(0, bot.plans.tier_for(guild_id).kb_pages - others)


async def sync_site(bot: RelayBot, guild_id: int, url: str, now: datetime) -> int:
    """Crawl a docs site and replace its pages in the knowledge base.
    Returns the number of pages stored. Raises CrawlError."""
    budget = await site_page_budget(bot, guild_id, url)
    if budget <= 0:
        raise CrawlError("your plan's website page limit is used up (see /relay plans)")
    pages = await Crawler(bot.crawl_http).crawl(url, max_pages=budget)
    if not pages:
        raise CrawlError("no readable pages found there")
    stored = await bot.kb.replace_source(guild_id, site_source(url), [(p.title, p.text, p.url) for p in pages])
    await bot.db.upsert_site(guild_id, url, stored, now)
    return stored


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

    @app_commands.command(name="sync", description="Import your docs website; Relay re-syncs it daily.")
    @app_commands.describe(url="Start page, e.g. https://example.com/docs/ (pages under it are included)")
    async def sync(self, interaction: discord.Interaction, url: str) -> None:
        root = normalize_url(url)
        if root is None:
            await interaction.response.send_message("Use a full `https://` URL.", ephemeral=True)
            return
        tier = self.bot.plans.tier_for(interaction.guild_id)
        if tier.kb_pages <= 0:
            await interaction.response.send_message(
                "Website sync isn't included in your plan. See `/relay plans`.",
                view=self.bot.plans.upsell_view(interaction.guild_id, include_topups=False) or discord.utils.MISSING,
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            stored = await sync_site(self.bot, interaction.guild_id, root, utcnow())
        except CrawlError as e:
            await interaction.followup.send(f"Couldn't sync {root}: {e}.", ephemeral=True)
            return
        budget = await site_page_budget(self.bot, interaction.guild_id, root)
        note = " (reached your plan's page limit; upgrade for more)" if stored >= budget else ""
        await interaction.followup.send(
            f"Imported **{stored}** page{'s' * (stored != 1)} from <{root}>{note}. Relay re-syncs it daily.",
            ephemeral=True,
        )

    @app_commands.command(name="sites", description="List synced websites.")
    async def sites(self, interaction: discord.Interaction) -> None:
        sites = await self.bot.db.sites(interaction.guild_id)
        tier = self.bot.plans.tier_for(interaction.guild_id)
        if not sites:
            msg = "No synced websites. Add one with `/kb sync`."
        else:
            used = sum(s["pages"] for s in sites)
            lines = [f"<{s['url']}> · {s['pages']} pages · synced {s['last_synced_at'] or 'never'}" for s in sites]
            msg = "\n".join(lines) + f"\n-# {used}/{tier.kb_pages} pages used on the {tier.name} plan"
        await interaction.response.send_message(msg[:2000], ephemeral=True)

    @app_commands.command(name="sync-remove", description="Stop syncing a website and remove its pages.")
    async def sync_remove(self, interaction: discord.Interaction, url: str) -> None:
        root = normalize_url(url) or url
        removed = await self.bot.kb.remove_source(interaction.guild_id, site_source(root))
        had_site = await self.bot.db.remove_site(interaction.guild_id, root)
        if had_site or removed:
            msg = f"Removed <{root}> and its {removed} page{'s' * (removed != 1)}."
        else:
            msg = "That website isn't synced. See `/kb sites`."
        await interaction.response.send_message(msg, ephemeral=True)

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
