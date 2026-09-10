from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vibetrading.agents.research.news_sources.base import NewsItem, NewsSource
from vibetrading.core.models import Stock


class MockNewsSource(NewsSource):
    """Deterministic canned headlines. Always available, no network/keys required."""

    async def fetch(self, stock: Stock) -> list[NewsItem]:
        now = datetime.now(UTC)
        name = stock.name or stock.symbol
        return [
            NewsItem(
                title=f"{name} reports quarterly results broadly in line with analyst estimates",
                summary="Revenue and margins were close to consensus; management reiterated full-year guidance.",
                source="mock-news",
                published_at=now - timedelta(hours=2),
            ),
            NewsItem(
                title=f"Brokerages maintain neutral stance on {name} ahead of sector outlook update",
                summary="Analysts cite balanced risk/reward at current valuation.",
                source="mock-news",
                published_at=now - timedelta(hours=9),
            ),
            NewsItem(
                title=f"{name} announces routine board meeting to consider fundraising options",
                summary="No material details disclosed; stock reaction expected to be limited.",
                source="mock-news",
                published_at=now - timedelta(hours=20),
            ),
        ]
