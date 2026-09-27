from datetime import UTC, datetime, timedelta

from relay.db import ANSWERED, DECLINED
from relay.plans import PlanService
from relay.support import Outcome, SupportService, looks_like_question

from .conftest import FakeRouter, json_response
from .test_plans import PRO, TOPUP, FakeEnt

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
REFUND_DOC = "Refunds are available within 14 days of purchase."


def service(db, kb, tiers, *responses):
    router = FakeRouter(*responses)
    plans = PlanService(db, tiers)
    return SupportService(db, kb, plans, router), router, plans


async def ask(svc, text, user_id=1, now=NOW):
    return await svc.handle_question(guild_id=1, channel_id=2, message_id=3, user_id=user_id, text=text, now=now)


async def calls_logged(db):
    async with db.conn.execute("SELECT tier, llm, input_tokens FROM llm_calls") as cur:
        return [tuple(r) for r in await cur.fetchall()]


def test_looks_like_question():
    assert not looks_like_question("thanks!")
    assert looks_like_question("How do I get a refund?")


async def test_answered_path_uses_tier_model_meters_and_logs(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    svc, router, _ = service(
        db, kb, tiers, json_response({"answerable": True, "answer": "Within 14 days.", "source_ids": [1]})
    )
    result = await ask(svc, "Can I get a refund?")
    assert result.outcome is Outcome.ANSWERED and result.source_titles == ("Refund policy",)
    assert (await db.get_question(result.question_id)).status == ANSWERED
    assert router.requested == ["free"]  # free tier -> free provider
    assert await db.usage_for(1, "2026-09") == 1
    assert await calls_logged(db) == [("free", "free", 1000)]


async def test_paid_tier_routes_to_its_claude_model(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    svc, router, plans = service(
        db, kb, tiers, json_response({"answerable": True, "answer": "Yes.", "source_ids": [1]})
    )
    plans.apply_entitlement(FakeEnt(1, PRO), NOW)
    await ask(svc, "Can I get a refund?")
    assert router.requested == ["claude-sonnet-5"]
    assert (await calls_logged(db))[0][:2] == ("pro", "claude-sonnet-5")


async def test_no_kb_match_declines_free_of_charge(db, kb, tiers):
    svc, router, _ = service(db, kb, tiers)
    result = await ask(svc, "What is the airspeed of a swallow?")
    assert result.outcome is Outcome.DECLINED and router.requested == []
    assert (await db.get_question(result.question_id)).status == DECLINED
    assert await db.usage_for(1, "2026-09") == 0


async def test_model_decline_is_recorded_and_metered(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    svc, _, _ = service(db, kb, tiers, json_response({"answerable": False, "answer": "", "source_ids": []}))
    result = await ask(svc, "Can I get a refund on a gift card?")
    assert result.outcome is Outcome.DECLINED
    assert await db.usage_for(1, "2026-09") == 1  # the AI call still cost money


async def test_api_error_is_recorded_but_not_metered(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    svc, _, _ = service(db, kb, tiers, json_response({}, stop_reason="refusal"))
    result = await ask(svc, "Can I get a refund?")
    assert result.outcome is Outcome.ERROR
    assert (await db.get_question(result.question_id)).status == DECLINED
    assert await db.usage_for(1, "2026-09") == 0 and await calls_logged(db) == []


async def test_cooldown_per_user(db, kb, tiers):
    svc, _, _ = service(db, kb, tiers)
    assert (await ask(svc, "first question here?")).outcome is Outcome.DECLINED
    assert (await ask(svc, "second question here?")).outcome is Outcome.COOLDOWN
    assert (await ask(svc, "other user question?", user_id=2)).outcome is Outcome.DECLINED
    later = NOW + timedelta(seconds=61)
    assert (await ask(svc, "third question here?", now=later)).outcome is Outcome.DECLINED


async def test_limit_blocks_until_topup(db, kb, tiers):
    await kb.add_doc(1, "Refund policy", REFUND_DOC, "manual")
    svc, _, plans = service(db, kb, tiers, json_response({"answerable": True, "answer": "Yes.", "source_ids": [1]}))
    for _ in range(3):  # test free tier allows 3
        await db.increment_usage(1, "2026-09")
    result = await ask(svc, "Can I get a refund?")
    assert result.outcome is Outcome.LIMITED and result.question_id is None

    await plans.redeem_topup(FakeEnt(1, TOPUP), 1, NOW)
    result = await ask(svc, "Can I get a refund?", now=NOW + timedelta(minutes=2))
    assert result.outcome is Outcome.ANSWERED
    assert await db.credit_balance(1) == 4
