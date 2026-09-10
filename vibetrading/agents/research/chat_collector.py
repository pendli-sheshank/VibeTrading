from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from vibetrading.agents.base import Agent
from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.agents.research.chat_sources.mock_source import MockChatSource
from vibetrading.agents.research.chat_sources.reddit_source import RedditChatSource
from vibetrading.agents.research.chat_sources.stocktwits_source import StockTwitsChatSource
from vibetrading.agents.research.chat_sources.telegram_source import TelegramChatSource
from vibetrading.agents.research.chat_sources.twitter_source import TwitterChatSource
from vibetrading.agents.research.chat_sources.valuepickr_source import ValuePickrChatSource
from vibetrading.agents.research.sentiment import score_sentiment
from vibetrading.config import Settings, get_settings
from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.llm.base import LLMAdapter
from vibetrading.llm.providers.mock_provider import MockLLMAdapter

logger = logging.getLogger(__name__)


def default_chat_sources(settings: Settings | None = None) -> list[ChatSource]:
    """MockChatSource is always included so there's always some signal in
    dev/paper mode; every real source is added only when its feature flag
    (and any required credential) is explicitly enabled — see the
    compliance note in the project plan.
    """
    settings = settings or get_settings()
    sources: list[ChatSource] = [MockChatSource()]

    if settings.chat_source_twitter_enabled and settings.twitter_bearer_token:
        sources.append(TwitterChatSource(bearer_token=settings.twitter_bearer_token))
    if settings.chat_source_reddit_enabled:
        sources.append(RedditChatSource())
    if settings.chat_source_telegram_enabled:
        sources.append(TelegramChatSource(api_id=settings.telegram_api_id, api_hash=settings.telegram_api_hash))
    if settings.chat_source_stocktwits_enabled:
        sources.append(StockTwitsChatSource())
    if settings.chat_source_valuepickr_enabled:
        sources.append(ValuePickrChatSource())

    return sources


class ChatCollectorAgent(Agent):
    agent_type = AgentType.CHAT

    def __init__(self, sources: list[ChatSource] | None = None, llm: LLMAdapter | None = None):
        self.sources = sources if sources is not None else default_chat_sources()
        self.llm = llm or MockLLMAdapter()

    async def analyze(self, stock: Stock, context: dict[str, Any]) -> AgentOutput:
        results = await asyncio.gather(
            *(source.fetch(stock) for source in self.sources), return_exceptions=True
        )

        messages: list[RawMessage] = []
        for source, result in zip(self.sources, results):
            if isinstance(result, Exception):
                logger.warning("Chat source %s failed for %s: %s", type(source).__name__, stock.symbol, result)
                continue
            messages.extend(result)

        summary, confidence = await score_sentiment(self.llm, stock, messages)

        return AgentOutput(
            agent_type=self.agent_type,
            stock_symbol=stock.symbol,
            timestamp=datetime.now(UTC),
            confidence=confidence,
            summary=summary,
            raw_data={
                "message_count": len(messages),
                "sources": [type(source).__name__ for source in self.sources],
            },
        )
