from datetime import UTC, datetime, timedelta

import discord

from relay.plans import PlanService, month_key
from relay.tiers import TierConfig

NOW = datetime(2026, 9, 27, tzinfo=UTC)
STARTER, PRO, TOPUP = 101, 102, 201


class FakeEnt:
    _next_id = 1000

    def __init__(self, guild_id, sku_id, *, deleted=False, ends_at=None, consumed=False, consume_fails=False):
        FakeEnt._next_id += 1
        self.id = FakeEnt._next_id
        self.guild_id, self.sku_id = guild_id, sku_id
        self.deleted, self.ends_at, self.consumed = deleted, ends_at, consumed
        self.consume_calls = 0
        self._consume_fails = consume_fails

    async def consume(self):
        self.consume_calls += 1
        if self._consume_fails:
            raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "fail")
        self.consumed = True


def test_unconfigured_skus_mean_self_hosted(db, tiers):
    unsold = TierConfig.model_validate(
        {
            "tier": [
                {"key": "free", "name": "Free", "monthly_answers": 5, "help_channels": 1, "llm": "free"},
                {"key": "pro", "name": "Pro", "sku_id": 0, "monthly_answers": 5, "help_channels": 2, "llm": "x"},
            ]
        }
    )
    plans = PlanService(db, unsold)
    tier = plans.tier_for(1)
    assert tier.name == "Self-hosted" and tier.monthly_answers is None and tier.llm == "x"
    assert plans.upsell_view(1) is None


async def test_highest_active_subscription_wins(db, tiers):
    plans = PlanService(db, tiers)
    assert plans.tier_for(1).key == "free"
    starter = FakeEnt(1, STARTER)
    plans.apply_entitlement(starter, NOW)
    assert plans.tier_for(1).key == "starter"
    plans.apply_entitlement(FakeEnt(1, PRO), NOW)
    assert plans.tier_for(1).key == "pro"
    plans.apply_entitlement(FakeEnt(2, 999), NOW)  # unknown SKU ignored
    assert plans.tier_for(2).key == "free"


async def test_ended_or_deleted_subscription_downgrades(db, tiers):
    plans = PlanService(db, tiers)
    ent = FakeEnt(1, PRO)
    plans.apply_entitlement(ent, NOW)
    ent.ends_at = NOW - timedelta(seconds=1)
    plans.apply_entitlement(ent, NOW)
    assert plans.tier_for(1).key == "free"
    ent2 = FakeEnt(1, STARTER)
    plans.apply_entitlement(ent2, NOW)
    ent2.deleted = True
    plans.apply_entitlement(ent2, NOW)
    assert plans.tier_for(1).key == "free"


async def test_allowance_monthly_then_credits_and_rollover(db, tiers):
    plans = PlanService(db, tiers)
    for _ in range(3):  # free tier allows 3
        await plans.consume(1, NOW)
    assert (await plans.allowance(1, NOW)).remaining == 0

    assert await plans.redeem_topup(FakeEnt(1, TOPUP), 1, NOW) == 5
    allowance = await plans.allowance(1, NOW)
    assert allowance.remaining == 5 and allowance.credits == 5
    await plans.consume(1, NOW)  # spends a credit, not monthly usage
    assert await db.credit_balance(1) == 4
    assert await db.usage_for(1, month_key(NOW)) == 3

    next_month = NOW + timedelta(days=5)
    assert (await plans.allowance(1, next_month)).remaining == 3 + 4  # fresh allowance, credits carry over


async def test_unlimited_tier_still_counts_usage(db, tiers):
    plans = PlanService(db, tiers)
    plans.apply_entitlement(FakeEnt(1, PRO), NOW)
    await plans.consume(1, NOW)
    allowance = await plans.allowance(1, NOW)
    assert allowance.remaining is None and allowance.used == 1


async def test_topup_redeemed_once_and_consumed(db, tiers):
    plans = PlanService(db, tiers)
    ent = FakeEnt(1, TOPUP)
    assert await plans.redeem_topup(ent, 1, NOW) == 5
    assert ent.consumed and ent.consume_calls == 1
    assert await plans.redeem_topup(ent, 1, NOW) is None  # replayed event
    assert await db.credit_balance(1) == 5
    assert await plans.redeem_topup(FakeEnt(1, STARTER), 1, NOW) is None  # not a top-up


async def test_topup_consume_failure_still_credits_once(db, tiers):
    plans = PlanService(db, tiers)
    ent = FakeEnt(1, TOPUP, consume_fails=True)
    assert await plans.redeem_topup(ent, 1, NOW) == 5
    assert await plans.redeem_topup(ent, 1, NOW) is None  # retry consumes, doesn't re-credit
    assert ent.consume_calls == 2 and await db.credit_balance(1) == 5


async def test_refresh_rebuilds_subs_and_catches_up_topups(db, tiers):
    plans = PlanService(db, tiers)
    plans.apply_entitlement(FakeEnt(1, PRO), NOW)
    pending_topup = FakeEnt(3, TOPUP)

    class FakeClient:
        def entitlements(self, *, skus, **kwargs):
            ids = {s.id for s in skus}

            async def gen():
                if STARTER in ids:
                    yield FakeEnt(2, STARTER)
                    yield FakeEnt(4, PRO, ends_at=NOW - timedelta(days=1))
                if TOPUP in ids:
                    yield pending_topup

            return gen()

    await plans.refresh(FakeClient(), NOW)
    assert [plans.tier_for(g).key for g in (1, 2, 4)] == ["free", "starter", "free"]
    assert await db.credit_balance(3) == 5


def test_upsell_offers_only_higher_tiers(db, tiers):
    plans = PlanService(db, tiers)
    plans.apply_entitlement(FakeEnt(1, STARTER), NOW)
    skus = [item.sku_id for item in plans.upsell_view(1).children]
    assert skus == [PRO, TOPUP]
    assert [i.sku_id for i in plans.upsell_view(1, include_topups=False).children] == [PRO]
    plans.apply_entitlement(FakeEnt(1, PRO), NOW)
    assert plans.upsell_view(1, include_topups=False) is None
