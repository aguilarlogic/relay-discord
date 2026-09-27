from unittest.mock import MagicMock

import discord

from relay.answer import AnswerResult
from relay.cogs.support import SupportCog, format_answer, thread_name
from relay.support import HandleResult, Outcome


def cog(help_ids):
    bot = MagicMock()
    bot.help_channel_ids = set(help_ids)
    return SupportCog(bot)


def message(channel, msg_id=1, type_=discord.MessageType.default):
    m = MagicMock(spec=discord.Message)
    m.channel, m.id, m.type = channel, msg_id, type_
    return m


def test_question_target_text_channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 10
    assert cog({10})._question_target(message(ch)) == "channel"
    assert cog({11})._question_target(message(ch)) is None
    assert cog({10})._question_target(message(ch, type_=discord.MessageType.pins_add)) is None


def test_question_target_forum_starter_only():
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 50, 10
    assert cog({10})._question_target(message(thread, msg_id=50)) == "forum"
    assert cog({10})._question_target(message(thread, msg_id=51)) is None  # follow-up reply
    assert cog({99})._question_target(message(thread, msg_id=50)) is None


def test_format_answer_includes_sources_and_fits():
    result = HandleResult(Outcome.ANSWERED, 1, AnswerResult(True, "x" * 1800, (1,)), source_titles=("Refunds", "FAQ"))
    text = format_answer(result)
    assert "Sources: Refunds · FAQ" in text and len(text) <= 2000


def test_thread_name():
    assert thread_name("  ") == "Question"
    assert len(thread_name("word " * 100)) == 91
