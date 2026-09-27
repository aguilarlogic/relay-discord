from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from relay.plans import FREE, PRO, SELF_HOSTED, PlanService, month_key

NOW = datetime(2026, 9, 27, tzinfo=UTC)
SKU = 555


def ent(guild_id, sku_id=SKU, deleted=False, ends_at=None):
    return SimpleNamespace(guild_id=guild_id, sku_id=sku_id, deleted=deleted, ends_at=ends_at)


async def test_no_sku_means_self_hosted_unlimited(db):
    plans = PlanService(db, None)
    assert plans.plan_for(1) is SELF_HOSTED
    assert await plans.remaining_answers(1, NOW) is None
    assert plans.upsell_view() is None


async def test_free_limit_and_month_rollover(db):
    plans = PlanService(db, SKU)
    assert plans.plan_for(1) is FREE
    for _ in range(FREE.monthly_answers):
        await plans.record_answer(1, NOW)
    assert await plans.remaining_answers(1, NOW) == 0
    next_month = NOW + timedelta(days=5)
    assert month_key(next_month) == "2026-10"
    assert await plans.remaining_answers(1, next_month) == FREE.monthly_answers


async def test_entitlement_events_toggle_pro(db):
    plans = PlanService(db, SKU)
    plans.apply_entitlement(ent(1), NOW)
    assert plans.plan_for(1) is PRO
    plans.apply_entitlement(ent(2, sku_id=999), NOW)  # other SKU ignored
    assert plans.plan_for(2) is FREE
    plans.apply_entitlement(ent(1, ends_at=NOW - timedelta(seconds=1)), NOW)
    assert plans.plan_for(1) is FREE
    plans.apply_entitlement(ent(1), NOW)
    plans.apply_entitlement(ent(1, deleted=True), NOW)
    assert plans.plan_for(1) is FREE


async def test_refresh_replaces_cache(db):
    plans = PlanService(db, SKU)
    plans.apply_entitlement(ent(1), NOW)

    class FakeClient:
        def entitlements(self, **kwargs):
            async def gen():
                yield ent(2)
                yield ent(3, ends_at=NOW - timedelta(days=1))

            return gen()

    await plans.refresh(FakeClient(), NOW)
    assert plans.plan_for(1) is FREE and plans.plan_for(2) is PRO and plans.plan_for(3) is FREE
