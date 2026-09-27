from datetime import UTC, datetime

from relay.db import ANSWERED, DECLINED, ESCALATED, SOLVED, Question
from relay.digest import build_digest, cluster_gaps, compute_stats, render_digest, render_stats
from relay.llm import ClaudeLLM

from .conftest import FakeAnthropic, json_response

NOW = datetime(2026, 9, 27, tzinfo=UTC).isoformat()


def q(i, status, text="How do I do the thing?", message_id=100):
    return Question(i, 1, 2, message_id, 3, text, status, (), NOW, None)


def test_stats_math():
    s = compute_stats({ANSWERED: 5, SOLVED: 3, ESCALATED: 2, DECLINED: 10})
    assert (s.total, s.answered, s.deflected) == (20, 10, 8)
    assert s.deflection_rate == 0.4
    assert round(s.hours_saved, 2) == round(8 * 5 / 60, 2)
    assert compute_stats({}).deflection_rate == 0.0


def test_render_stats_empty_and_full():
    assert "No questions" in render_stats(compute_stats({}), 7)
    text = render_stats(compute_stats({ANSWERED: 1, DECLINED: 1}), 7)
    assert "Last 7 days" in text and "50%" in text


def test_digest_lists_escalations_and_unanswered_with_links():
    digest = build_digest([q(1, ESCALATED, "Server is down"), q(2, DECLINED, "Where is the API key", None)])
    text = render_digest(digest, guild_id=9)
    assert "https://discord.com/channels/9/2/100" in text
    assert "https://discord.com/channels/9/2)" in text  # /ask questions have no message id
    assert len(text) <= 2000


async def test_cluster_gaps_skips_api_for_few_questions():
    fake = FakeAnthropic()
    gaps, usage = await cluster_gaps(ClaudeLLM(fake, "claude-haiku-4-5"), [q(1, DECLINED), q(2, DECLINED)])
    assert len(gaps) == 2 and usage is None and fake.messages.calls == []


async def test_cluster_gaps_uses_model_and_falls_back_on_error():
    declined = [q(i, DECLINED, f"question {i} about billing") for i in range(5)]
    fake = FakeAnthropic(json_response({"topics": [{"topic": "Billing FAQ", "count": 5, "example": "billing?"}]}))
    gaps, usage = await cluster_gaps(ClaudeLLM(fake, "claude-haiku-4-5"), declined)
    assert gaps[0].topic == "Billing FAQ" and gaps[0].count == 5 and usage.input_tokens == 1000

    broken = FakeAnthropic(json_response({}, stop_reason="refusal"))
    gaps, usage = await cluster_gaps(ClaudeLLM(broken, "claude-haiku-4-5"), declined)
    assert len(gaps) == 5 and usage is None
