from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vibetrading.agents.base import Agent
from vibetrading.agents.research.news_sources.base import NewsSource
from vibetrading.agents.research.news_sources.mock_source import MockNewsSource
from vibetrading.agents.research.news_sources.news_api_source import NewsAPISource
from vibetrading.agents.research.news_summarizer import summarize_news
from vibetrading.config import Settings, get_settings
from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.llm.base import LLMAdapter
from vibetrading.llm.providers.mock_provider import MockLLMAdapter


def default_news_source(settings: Settings | None = None) -> NewsSource:
    settings = settings or get_settings()
    if settings.news_source_enabled and settings.news_api_key:
        return NewsAPISource(api_key=settings.news_api_key)
    return MockNewsSource()


class NewsCollectorAgent(Agent):
    agent_type = AgentType.NEWS

    def __init__(self, source: NewsSource | None = None, llm: LLMAdapter | None = None):
        self.source = source or default_news_source()
        self.llm = llm or MockLLMAdapter()

    async def analyze(self, stock: Stock, context: dict[str, Any]) -> AgentOutput:
        items = await self.source.fetch(stock)
        summary, confidence = await summarize_news(self.llm, stock, items)

        return AgentOutput(
            agent_type=self.agent_type,
            stock_symbol=stock.symbol,
            timestamp=datetime.now(UTC),
            confidence=confidence,
            summary=summary,
            raw_data={
                "item_count": len(items),
                "items": [item.model_dump(mode="json") for item in items],
            },
        )
