from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vibetrading.agents.research.news_sources.base import NewsItem, NewsSource
from vibetrading.core.models import Stock


class NewsAPISource(NewsSource):
    """Wraps NewsAPI.org's /v2/everything endpoint.

    Gated by NEWS_SOURCE_ENABLED + NEWS_API_KEY (see .env.example) — the
    factory in news_collector.py only constructs this when both are set;
    fetch() also degrades to an empty list if api_key is missing so this
    class is never the reason a caller crashes.
    """

    BASE_URL = "https://newsapi.org/v2/everything"

    def __init__(self, api_key: str, http_client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self._client = http_client

    async def fetch(self, stock: Stock) -> list[NewsItem]:
        if not self.api_key:
            return []

        query = stock.name or stock.symbol
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
            "apiKey": self.api_key,
        }

        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        finally:
            if owns_client:
                await client.aclose()

        items: list[NewsItem] = []
        for article in data.get("articles", []):
            items.append(
                NewsItem(
                    title=article.get("title") or "",
                    summary=article.get("description") or "",
                    source=(article.get("source") or {}).get("name") or "newsapi",
                    url=article.get("url") or "",
                    published_at=_parse_timestamp(article.get("publishedAt")),
                )
            )
        return items


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)
