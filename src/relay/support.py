"""The question -> answer pipeline, free of Discord I/O so it can be tested
directly. The support cog handles the Discord side (threads, buttons)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from relay.answer import AnswerResult, answer_question
from relay.db import ANSWERED, DECLINED, Database
from relay.kb import KnowledgeBase
from relay.llm import LLMError, LLMRouter
from relay.plans import PlanService

logger = logging.getLogger(__name__)

MIN_QUESTION_CHARS = 15
USER_COOLDOWN_SECONDS = 60.0


class Outcome(StrEnum):
    COOLDOWN = "cooldown"  # same user asked too recently; nothing recorded
    LIMITED = "limited"  # monthly allowance and top-ups used up; nothing recorded
    DECLINED = "declined"  # no KB match or model said it can't answer; recorded
    ERROR = "error"  # AI provider failure or quota; recorded as declined so staff see it
    ANSWERED = "answered"


@dataclass(frozen=True)
class HandleResult:
    outcome: Outcome
    question_id: int | None = None
    answer: AnswerResult | None = None
    source_titles: tuple[str, ...] = ()


def looks_like_question(text: str) -> bool:
    return len(text.strip()) >= MIN_QUESTION_CHARS


class SupportService:
    def __init__(self, db: Database, kb: KnowledgeBase, plans: PlanService, router: LLMRouter) -> None:
        self.db = db
        self.kb = kb
        self.plans = plans
        self.router = router
        self._last_asked: dict[tuple[int, int], float] = {}

    def _on_cooldown(self, guild_id: int, user_id: int, now: datetime) -> bool:
        key = (guild_id, user_id)
        ts = now.timestamp()
        last = self._last_asked.get(key)
        if last is not None and ts - last < USER_COOLDOWN_SECONDS:
            return True
        self._last_asked[key] = ts
        if len(self._last_asked) > 10_000:  # bound memory on huge deployments
            cutoff = ts - USER_COOLDOWN_SECONDS
            self._last_asked = {k: v for k, v in self._last_asked.items() if v >= cutoff}
        return False

    async def handle_question(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int | None,
        user_id: int,
        text: str,
        now: datetime,
    ) -> HandleResult:
        if self._on_cooldown(guild_id, user_id, now):
            return HandleResult(Outcome.COOLDOWN)

        allowance = await self.plans.allowance(guild_id, now)
        if allowance.remaining == 0:
            return HandleResult(Outcome.LIMITED)

        common = dict(guild_id=guild_id, channel_id=channel_id, message_id=message_id, user_id=user_id)
        # No matching docs means no AI call, so it's free and not metered.
        chunks = await self.kb.search(guild_id, text)
        outcome = Outcome.DECLINED
        result: AnswerResult | None = None
        if chunks:
            tier = self.plans.tier_for(guild_id)
            try:
                result, usage = await answer_question(self.router.get(tier.llm), text, chunks)
            except LLMError:
                logger.exception("answering failed in guild %s (tier %s)", guild_id, tier.key)
                outcome = Outcome.ERROR
            else:
                # Every completed AI call is metered, including ones where the
                # model declines -- they cost the same.
                await self.plans.consume(guild_id, now)
                await self.db.log_llm_call(
                    guild_id=guild_id,
                    tier=tier.key,
                    llm=tier.llm,
                    purpose="answer",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    now=now,
                )

        if result is None or not result.answerable:
            qid = await self.db.record_question(**common, text=text, status=DECLINED, now=now)
            return HandleResult(outcome, question_id=qid)

        qid = await self.db.record_question(
            **common, text=text, status=ANSWERED, source_doc_ids=result.source_doc_ids, now=now
        )
        titles_by_id = await self.kb.doc_titles(guild_id, list(result.source_doc_ids))
        titles = tuple(titles_by_id[d] for d in result.source_doc_ids if d in titles_by_id)
        return HandleResult(Outcome.ANSWERED, question_id=qid, answer=result, source_titles=titles)
