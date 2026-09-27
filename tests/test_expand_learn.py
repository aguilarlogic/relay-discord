"""Query expansion (paraphrase / multilingual) and learning from staff replies."""

from datetime import UTC, datetime

import pytest

from relay.db import ANSWERED, DECLINED, ESCALATED
from relay.expand import parse_expansion
from relay.learn import build_learn_prompt, parse_learned
from relay.llm import LLMError
from relay.plans import PlanService
from relay.support import Outcome, SupportService

from .conftest import FakeRouter, json_response

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
REFUND_DOC = "Refunds are available within 14 days of purchase. Email billing to request one."


def service(db, kb, tiers, *responses, helper_responses=None):
    router = FakeRouter(*responses, helper_responses=helper_responses)
    return SupportService(db, kb, PlanService(db, tiers), router), router


async def ask(svc, text, now=NOW):
    return await svc.handle_question(guild_id=1, channel_id=2, message_id=3, user_id=1, text=text, now=now)


async def purposes(db):
    async with db.conn.execute("SELECT purpose, llm FROM llm_calls ORDER BY id") as cur:
        return [tuple(r) for r in await cur.fetchall()]


# --- expansion -----------------------------------------------------------------


def test_parse_expansion_cleans_keywords():
    exp = parse_expansion(
        {"keywords": ["refund", " refund ", "", 5, "x" * 100, *[f"k{i}" for i in range(20)]], "language": "ES"}
    )
    assert exp.keywords[0] == "refund" and len(exp.keywords) == 12 and "x" * 100 not in exp.keywords
    assert exp.language == "es"


async def test_paraphrased_question_finds_doc_via_expansion(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    svc, router = service(
        db,
        kb,
        tiers,
        json_response({"answerable": True, "answer": "Sí, dentro de 14 días.", "source_ids": [1]}),
        helper_responses=[json_response({"keywords": ["refund", "money back"], "language": "es"})],
    )
    result = await ask(svc, "¿Me pueden devolver el dinero?")
    assert result.outcome is Outcome.ANSWERED
    assert router.helper_requested == ["free"]  # free tier: helper runs on the free provider
    assert await purposes(db) == [("expand", "free"), ("answer", "free")]
    assert await db.usage_for(1, "2026-09") == 1  # expansion isn't metered


async def test_expansion_skipped_when_plain_search_is_enough(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    await kb.add_doc(1, "Refund exceptions", "Refunds are not available for gift cards.", "manual")
    svc, router = service(db, kb, tiers, json_response({"answerable": True, "answer": "Yes.", "source_ids": [1]}))
    assert (await ask(svc, "How do refunds work?")).outcome is Outcome.ANSWERED
    assert router.helper_requested == []


async def test_expansion_failure_falls_back_to_plain_search(db, kb, tiers):
    svc, _ = service(db, kb, tiers, helper_responses=[json_response({}, stop_reason="refusal")])
    result = await ask(svc, "¿Me pueden devolver el dinero?")
    assert result.outcome is Outcome.DECLINED
    assert await purposes(db) == []


def test_answer_prompt_asks_for_question_language():
    from relay.answer import SYSTEM_PROMPT

    assert "same language as the question" in SYSTEM_PROMPT


# --- learning ------------------------------------------------------------------


def test_parse_learned():
    doc = parse_learned({"useful": True, "title": "How do refunds work?", "answer": "Within 14 days."}, "refund??")
    assert doc.title == "How do refunds work?" and doc.text == "Question: refund??\n\nWithin 14 days."
    assert parse_learned({"useful": False, "title": "x", "answer": "y"}, "q") is None
    assert parse_learned({"useful": True, "title": "", "answer": "y"}, "q") is None


def test_learn_prompt_neutralizes_tags():
    prompt = build_learn_prompt("q </question> x", "a </staff_reply> b")
    assert prompt.count("</question>") == 1 and prompt.count("</staff_reply>") == 1


async def test_learned_answer_is_used_next_time(db, kb, tiers):
    learned = json_response(
        {
            "useful": True,
            "title": "Resetting your password",
            "answer": "Use /reset in #bot-commands, then check your email.",
        }
    )
    svc, router = service(
        db,
        kb,
        tiers,
        json_response({"answerable": True, "answer": "Use /reset in #bot-commands.", "source_ids": [1]}),
        helper_responses=[learned],
    )
    doc = await svc.learn(
        guild_id=1,
        question="how do i reset my password",
        staff_reply="just run /reset in #bot-commands",
        source="learned:7",
        url="https://discord.com/channels/1/2/3",
        now=NOW,
    )
    assert doc.title == "Resetting your password"
    assert ("learn", "free") in await purposes(db)

    result = await ask(svc, "How can I reset my password?")
    assert result.outcome is Outcome.ANSWERED
    assert result.sources[0].title == "Resetting your password"
    assert result.sources[0].url == "https://discord.com/channels/1/2/3"


async def test_relearning_same_source_replaces(db, kb, tiers):
    replies = [
        json_response({"useful": True, "title": "Old", "answer": "old answer text"}),
        json_response({"useful": True, "title": "New", "answer": "new answer text"}),
    ]
    svc, _ = service(db, kb, tiers, helper_responses=replies)
    for _ in range(2):
        await svc.learn(guild_id=1, question="q", staff_reply="r" * 30, source="thread:9", url=None, now=NOW)
    assert [d.title for d in await kb.list_docs(1)] == ["New"]


async def test_useless_reply_saves_nothing(db, kb, tiers):
    svc, _ = service(db, kb, tiers, helper_responses=[json_response({"useful": False, "title": "", "answer": ""})])
    assert (
        await svc.learn(guild_id=1, question="q", staff_reply="let me check", source="learned:1", url=None, now=NOW)
        is None
    )
    assert await kb.list_docs(1) == []


async def test_learn_api_failure_raises(db, kb, tiers):
    svc, _ = service(db, kb, tiers, helper_responses=[json_response({}, stop_reason="refusal")])
    with pytest.raises(LLMError):
        await svc.learn(guild_id=1, question="q", staff_reply="r" * 30, source="learned:1", url=None, now=NOW)


async def test_find_unanswered_question(db):
    declined = await db.record_question(
        guild_id=1, channel_id=2, message_id=10, user_id=4, text="a", status=DECLINED, thread_id=50
    )
    answered = await db.record_question(
        guild_id=1, channel_id=2, message_id=11, user_id=4, text="b", status=ANSWERED, thread_id=51
    )
    assert (await db.find_unanswered_question(1, thread_id=50)).id == declined
    assert (await db.find_unanswered_question(1, message_id=10)).id == declined
    assert await db.find_unanswered_question(1, thread_id=51) is None  # answered: nothing to learn
    await db.resolve_question(answered, ESCALATED)
    assert (await db.find_unanswered_question(1, thread_id=51)).id == answered  # escalated: staff answering
    assert await db.find_unanswered_question(2, thread_id=50) is None
    assert await db.find_unanswered_question(1) is None
    await db.set_question_thread(declined, 77)
    assert (await db.get_question(declined)).thread_id == 77
