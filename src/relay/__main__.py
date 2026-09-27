"""Entry point: `relay` or `python -m relay`."""

from __future__ import annotations

import logging

import discord

from relay.bot import RelayBot
from relay.config import Settings


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    discord.utils.setup_logging(level=logging.INFO)
    bot = RelayBot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
