from pathlib import Path

import pytest
from pydantic import ValidationError

from relay.tiers import TierConfig, load_tiers

REPO_TIERS = Path(__file__).resolve().parent.parent / "tiers.toml"


def test_shipped_tiers_file_is_valid_and_not_yet_monetized():
    tiers = load_tiers(REPO_TIERS)
    assert tiers.free.llm == "free" and tiers.free.sku_id is None
    assert not tiers.monetized  # sku_ids ship as 0 until the operator sets them
    assert [t.key for t in tiers.tier] == ["free", "starter", "pro", "business"]
    for t in tiers.tier:
        assert t.llm in tiers.prices, f"no price for {t.llm}"


def test_worst_case_cost_leaves_margin_in_shipped_tiers():
    """Guard rail: at ~3k input / 800 output tokens per call (a pessimistic
    answer), a paid tier maxing out its allowance must cost < 60% of its price."""
    tiers = load_tiers(REPO_TIERS)
    for t in tiers.tier[1:]:
        worst = tiers.estimate_cost(t.llm, 3000, 800) * t.monthly_answers
        assert worst < 0.6 * t.monthly_price_usd, f"{t.key}: ${worst:.2f} vs ${t.monthly_price_usd}"


def test_topups_leave_margin_even_on_the_priciest_tier():
    tiers = load_tiers(REPO_TIERS)
    worst_per_call = max(tiers.estimate_cost(t.llm, 3000, 800) for t in tiers.tier)
    for pack in tiers.topup:
        price = float(pack.price_label.lstrip("$"))
        assert worst_per_call * pack.answers < 0.75 * price, pack.name


def test_unlimited_parses_to_none(tiers):
    pro = tiers.tier[-1]
    assert pro.monthly_answers is None and pro.help_channels is None


def base(**overrides):
    tier = [
        {"key": "free", "name": "Free", "monthly_answers": 5, "help_channels": 1, "llm": "free"},
        {"key": "pro", "name": "Pro", "sku_id": 1, "monthly_answers": 5, "help_channels": 1, "llm": "x"},
    ]
    return {"tier": tier, **overrides}


def test_free_tier_must_not_have_sku():
    data = base()
    data["tier"][0]["sku_id"] = 5
    with pytest.raises(ValidationError):
        TierConfig.model_validate(data)


def test_duplicate_skus_rejected():
    with pytest.raises(ValidationError):
        TierConfig.model_validate(base(topup=[{"sku_id": 1, "name": "x", "answers": 5}]))


def test_limits_must_be_positive():
    data = base()
    data["tier"][1]["monthly_answers"] = 0
    with pytest.raises(ValidationError):
        TierConfig.model_validate(data)


def test_helpers(tiers):
    assert tiers.cheapest_with("kb_upload").key == "starter"
    assert tiers.cheapest_with("digest").key == "pro"
    assert tiers.topup_by_sku(201).answers == 5
    assert tiers.estimate_cost("claude-sonnet-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert tiers.estimate_cost("unknown-model", 10, 10) == 0.0
