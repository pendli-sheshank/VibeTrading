from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vibetrading.agents.base import Agent
from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.llm.base import LLMAdapter, Message
from vibetrading.llm.parsing import parse_json_response
from vibetrading.llm.providers.mock_provider import MockLLMAdapter

_SYSTEM_PROMPT = (
    "You are a research analyst covering Indian equities. Using web search, "
    "find recent news and social/forum sentiment (X/Twitter, Reddit, "
    "StockTwits, investor forums) for the given stock, then write 2-3 "
    "sentences combining both into a single picture (agreement, conflict, "
    "and overall lean). Give a confidence score from 0 to 1 for how strong "
    "and consistent the combined signal is (0 = no information found or "
    "purely mixed/noisy, 1 = strong and consistent across sources). "
    'Respond ONLY as JSON: {"summary": string, "confidence": number}.'
)


class ResearchAgent(Agent):
    """Gathers news + social/forum sentiment for a stock via the LLM's own
    hosted web search (LLMAdapter.complete(enable_web_search=True)) and
    synthesizes it into one combined research view, in a single LLM call --
    no dedicated news/social API integrations to configure or maintain.
    """

    agent_type = AgentType.RESEARCH
    run_interval_seconds = 900

    def __init__(self, llm: LLMAdapter | None = None):
        self.llm = llm or MockLLMAdapter()

    async def analyze(self, stock: Stock, context: dict[str, Any]) -> AgentOutput:
        user_prompt = f"Stock: {stock.symbol} ({stock.name or stock.symbol}), {stock.exchange}"
        response = await self.llm.complete(
            system=_SYSTEM_PROMPT,
            messages=[Message(role="user", content=user_prompt)],
            enable_web_search=True,
        )
        data = parse_json_response(response.content)

        if data and isinstance(data.get("summary"), str):
            summary = data["summary"]
            confidence = max(0.0, min(1.0, _to_float(data.get("confidence"), default=0.4)))
        else:
            # Fallback: no clean JSON, but still return a usable view rather
            # than failing the whole Research Agent run.
            summary = response.content.strip() or f"No research summary available for {stock.symbol}."
            confidence = 0.2

        return AgentOutput(
            agent_type=self.agent_type,
            stock_symbol=stock.symbol,
            timestamp=datetime.now(UTC),
            confidence=confidence,
            summary=summary,
            raw_data={"model": response.model},
        )


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
