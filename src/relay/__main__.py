"""Entry point: `relay` runs the bot; `relay costs` prints the AI spend report."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import timedelta
from pathlib import Path

import discord

from relay.costs import render_report, summarize
from relay.db import Database, utcnow
from relay.tiers import load_tiers


async def _costs(db_path: Path, tiers_path: Path, days: int) -> None:
    tiers = load_tiers(tiers_path)
    db = await Database.open(db_path)
    try:
        rows = await db.llm_usage_since(utcnow() - timedelta(days=days))
    finally:
        await db.close()
    print(render_report(summarize(rows, tiers, days), days))


def main() -> None:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="command")
    costs = sub.add_parser("costs", help="show AI spend and margin per tier")
    costs.add_argument("--days", type=int, default=30)
    costs.add_argument("--db", type=Path, default=Path(os.environ.get("DATABASE_PATH", "data/relay.db")))
    costs.add_argument("--tiers", type=Path, default=Path(os.environ.get("RELAY_TIERS_PATH", "tiers.toml")))
    args = parser.parse_args()

    if args.command == "costs":
        asyncio.run(_costs(args.db, args.tiers, args.days))
        return

    from relay.bot import RelayBot
    from relay.config import Settings

    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    discord.utils.setup_logging(level=logging.INFO)
    bot = RelayBot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
