from datetime import UTC, datetime, timedelta

from relay.answer import Answerer
from relay.db import ANSWERED, DECLINED
from relay.llm import StructuredLLM
from relay.plans import FREE, PlanService
from relay.support import Outcome, SupportService, looks_like_question

from .conftest import FakeAnthropic, json_response

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def service(db, kb, *responses, sku=None):
    fake = FakeAnthropic(*responses)
    svc = SupportService(db, kb, Answerer(StructuredLLM(fake, "claude-opus-5")), PlanService(db, sku))
    return svc, fake


async def ask(svc, text, user_id=1, now=NOW):
    return await svc.handle_question(guild_id=1, channel_id=2, message_id=3, user_id=user_id, text=text, now=now)


def test_looks_like_question():
    assert not looks_like_question("thanks!")
    assert looks_like_question("How do I get a refund?")


async def test_answered_path_records_and_meters(db, kb):
    await kb.add_doc(1, "Refund policy", "Refunds are available within 14 days of purchase.", "manual")
    svc, fake = service(db, kb, json_response({"answerable": True, "answer": "Within 14 days.", "source_ids": [1]}))
    result = await ask(svc, "Can I get a refund?")
    assert result.outcome is Outcome.ANSWERED
    assert result.source_titles == ("Refund policy",)
    assert (await db.get_question(result.question_id)).status == ANSWERED
    assert await db.usage_for(1, "2026-09") == 1


async def test_no_kb_match_declines_without_api_call(db, kb):
    svc, fake = service(db, kb)
    result = await ask(svc, "What is the airspeed of a swallow?")
    assert result.outcome is Outcome.DECLINED and fake.messages.calls == []
    assert (await db.get_question(result.question_id)).status == DECLINED
    assert await db.usage_for(1, "2026-09") == 0


async def test_model_decline_is_recorded_not_metered(db, kb):
    await kb.add_doc(1, "Refund policy", "Refunds are available within 14 days.", "manual")
    svc, _ = service(db, kb, json_response({"answerable": False, "answer": "", "source_ids": []}))
    result = await ask(svc, "Can I get a refund on a gift card?")
    assert result.outcome is Outcome.DECLINED
    assert await db.usage_for(1, "2026-09") == 0


async def test_api_error_is_recorded_as_declined(db, kb):
    await kb.add_doc(1, "Refund policy", "Refunds are available within 14 days.", "manual")
    svc, _ = service(db, kb, json_response({}, stop_reason="refusal"))
    result = await ask(svc, "Can I get a refund?")
    assert result.outcome is Outcome.ERROR
    assert (await db.get_question(result.question_id)).status == DECLINED


async def test_cooldown_per_user(db, kb):
    svc, _ = service(db, kb)
    assert (await ask(svc, "first question here?")).outcome is Outcome.DECLINED
    assert (await ask(svc, "second question here?")).outcome is Outcome.COOLDOWN
    assert (await ask(svc, "other user question?", user_id=2)).outcome is Outcome.DECLINED
    later = NOW + timedelta(seconds=61)
    assert (await ask(svc, "third question here?", now=later)).outcome is Outcome.DECLINED


async def test_free_limit_blocks_before_any_work(db, kb):
    svc, fake = service(db, kb, sku=555)
    for _ in range(FREE.monthly_answers):
        await db.increment_usage(1, "2026-09")
    result = await ask(svc, "Can I get a refund?")
    assert result.outcome is Outcome.LIMITED and result.question_id is None
