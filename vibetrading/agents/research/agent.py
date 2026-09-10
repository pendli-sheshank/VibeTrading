from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from vibetrading.agents.base import Agent
from vibetrading.agents.research.chat_collector import ChatCollectorAgent, default_chat_sources
from vibetrading.agents.research.news_collector import NewsCollectorAgent, default_news_source
from vibetrading.config import Settings
from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.llm.base import LLMAdapter, Message
from vibetrading.llm.parsing import parse_json_response
from vibetrading.llm.providers.mock_provider import MockLLMAdapter

_SYSTEM_PROMPT = (
    "You are a research analyst combining a news summary and a social-media "
    "sentiment summary for an Indian stock into a single research view. "
    "Write 2-3 sentences describing the combined picture (agreement, "
    "conflict, and overall lean), then give a confidence score from 0 to 1 "
    "for how strong and consistent the combined signal is. "
    'Respond ONLY as JSON: {"summary": string, "confidence": number}.'
)


class ResearchAgent(Agent):
    """Combines the News and Chat collectors into one Research Agent output.

    news_collector and chat_collector each independently produce their own
    (persistable, testable) AgentOutput; this facade runs them concurrently
    and uses an LLM call to synthesize a single combined research view,
    which is what the Strategy Agent consumes downstream.
    """

    agent_type = AgentType.RESEARCH
    run_interval_seconds = 900

    def __init__(
        self,
        news_agent: NewsCollectorAgent | None = None,
        chat_agent: ChatCollectorAgent | None = None,
        llm: LLMAdapter | None = None,
        settings: Settings | None = None,
    ):
        self.news_agent = news_agent or NewsCollectorAgent(source=default_news_source(settings))
        self.chat_agent = chat_agent or ChatCollectorAgent(sources=default_chat_sources(settings))
        self.llm = llm or MockLLMAdapter()

    async def analyze(self, stock: Stock, context: dict[str, Any]) -> AgentOutput:
        news_output, chat_output = await asyncio.gather(
            self.news_agent.analyze(stock, context),
            self.chat_agent.analyze(stock, context),
        )

        summary, confidence = await _combine(self.llm, stock, news_output, chat_output)

        return AgentOutput(
            agent_type=self.agent_type,
            stock_symbol=stock.symbol,
            timestamp=datetime.now(UTC),
            confidence=confidence,
            summary=summary,
            raw_data={
                "news": news_output.model_dump(mode="json"),
                "chat": chat_output.model_dump(mode="json"),
            },
        )


async def _combine(
    llm: LLMAdapter, stock: Stock, news_output: AgentOutput, chat_output: AgentOutput
) -> tuple[str, float]:
    user_prompt = (
        f"Stock: {stock.symbol}\n"
        f"News summary (confidence {news_output.confidence:.2f}): {news_output.summary}\n"
        f"Social sentiment summary (confidence {chat_output.confidence:.2f}): {chat_output.summary}"
    )
    response = await llm.complete(system=_SYSTEM_PROMPT, messages=[Message(role="user", content=user_prompt)])
    data = parse_json_response(response.content)

    if data and isinstance(data.get("summary"), str):
        confidence = max(0.0, min(1.0, _to_float(data.get("confidence"), default=0.4)))
        return data["summary"], confidence

    # Fallback: no reasoning synthesis available, but still return a usable
    # combined view rather than failing the whole Research Agent run.
    fallback_summary = f"News: {news_output.summary} | Chat: {chat_output.summary}"
    fallback_confidence = round((news_output.confidence + chat_output.confidence) / 2, 2)
    return fallback_summary, fallback_confidence


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
