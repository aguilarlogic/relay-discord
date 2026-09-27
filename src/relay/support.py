"""The question -> answer pipeline, free of Discord I/O so it can be tested
directly. The support cog handles the Discord side (threads, buttons)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from relay.answer import AnswerResult, answer_question
from relay.db import ANSWERED, DECLINED, Database
from relay.expand import expand_query
from relay.kb import DocRef, KnowledgeBase
from relay.learn import LearnedDoc, learn_from_reply
from relay.llm import LLMError, LLMResult, LLMRouter
from relay.plans import PlanService

logger = logging.getLogger(__name__)

MIN_QUESTION_CHARS = 15
USER_COOLDOWN_SECONDS = 60.0
# Run query expansion only when plain keyword search finds fewer chunks than
# this -- a question worded like the docs doesn't need it.
EXPAND_BELOW = 2


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
    sources: tuple[DocRef, ...] = ()


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
        thread_id: int | None = None,
    ) -> HandleResult:
        if self._on_cooldown(guild_id, user_id, now):
            return HandleResult(Outcome.COOLDOWN)

        allowance = await self.plans.allowance(guild_id, now)
        if allowance.remaining == 0:
            return HandleResult(Outcome.LIMITED)

        common = dict(
            guild_id=guild_id, channel_id=channel_id, message_id=message_id, user_id=user_id, thread_id=thread_id
        )
        tier = self.plans.tier_for(guild_id)
        chunks = await self.kb.search(guild_id, text)
        if len(chunks) < EXPAND_BELOW:
            # Different wording or another language than the docs: ask the
            # cheap model for English search keywords and search again.
            keywords = await self._expand(guild_id, tier.key, tier.llm, text, now)
            if keywords:
                chunks = await self.kb.search(guild_id, text, extra_terms=keywords)

        # No matching docs means no answer call, so it's free and not metered.
        outcome = Outcome.DECLINED
        result: AnswerResult | None = None
        if chunks:
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
        refs = await self.kb.doc_refs(guild_id, list(result.source_doc_ids))
        sources = tuple(refs[d] for d in result.source_doc_ids if d in refs)
        return HandleResult(Outcome.ANSWERED, question_id=qid, answer=result, sources=sources)

    async def _log(self, guild_id: int, tier: str, llm: str, purpose: str, usage: LLMResult, now: datetime) -> None:
        await self.db.log_llm_call(
            guild_id=guild_id,
            tier=tier,
            llm=llm,
            purpose=purpose,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            now=now,
        )

    async def _expand(self, guild_id: int, tier_key: str, tier_llm: str, text: str, now: datetime) -> tuple[str, ...]:
        """Helper call, logged for cost reports but not metered against the
        answer allowance. Failure just means searching with the original text."""
        try:
            key, llm = self.router.helper(tier_llm)
            expansion, usage = await expand_query(llm, text)
        except LLMError:
            logger.warning("query expansion failed in guild %s; using plain search", guild_id)
            return ()
        await self._log(guild_id, tier_key, key, "expand", usage, now)
        return expansion.keywords

    async def learn(
        self, *, guild_id: int, question: str, staff_reply: str, source: str, url: str | None, now: datetime
    ) -> LearnedDoc | None:
        """Turn a staff reply into a knowledge-base doc (replacing any earlier
        doc from the same source). Returns None if the reply isn't a reusable
        answer. Raises LLMError if the AI call fails."""
        tier = self.plans.tier_for(guild_id)
        key, llm = self.router.helper(tier.llm)
        doc, usage = await learn_from_reply(llm, question, staff_reply)
        await self._log(guild_id, tier.key, key, "learn", usage, now)
        if doc is not None:
            await self.kb.replace_source(guild_id, source, [(doc.title, doc.text, url)])
        return doc
