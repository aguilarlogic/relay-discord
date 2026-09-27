from datetime import UTC, datetime, timedelta

from relay.costs import render_report, summarize

NOW = datetime(2026, 9, 27, tzinfo=UTC)


async def test_cost_report_from_logged_calls(db, tiers):
    for guild in (1, 2):
        for _ in range(10):
            await db.log_llm_call(
                guild_id=guild,
                tier="pro",
                llm="claude-sonnet-5",
                purpose="answer",
                input_tokens=2000,
                output_tokens=500,
                now=NOW,
            )
    await db.log_llm_call(
        guild_id=3, tier="free", llm="free", purpose="answer", input_tokens=2000, output_tokens=500, now=NOW
    )
    await db.log_llm_call(  # outside the window
        guild_id=3,
        tier="free",
        llm="free",
        purpose="answer",
        input_tokens=1,
        output_tokens=1,
        now=NOW - timedelta(days=60),
    )
    rows = await db.llm_usage_since(NOW - timedelta(days=30))
    costs = {c.tier: c for c in summarize(rows, tiers, days=30)}

    pro = costs["pro"]
    assert (pro.calls, pro.guilds) == (20, 2)
    per_call = (2000 * 2 + 500 * 10) / 1_000_000
    assert abs(pro.cost_usd - 20 * per_call) < 1e-9
    assert abs(pro.monthly_margin_per_guild - (19.99 - 10 * per_call)) < 1e-9
    assert costs["free"].cost_usd == 0 and costs["free"].calls == 1

    report = render_report(list(costs.values()), 30)
    assert "pro" in report and "Total AI cost" in report


def test_empty_report():
    assert "No AI calls" in render_report([], 7)
