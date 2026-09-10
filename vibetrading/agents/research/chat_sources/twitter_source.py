from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.core.models import Stock


class TwitterChatSource(ChatSource):
    """X/Twitter API v2 recent-search endpoint. Requires a bearer token with
    at least Basic-tier access. Gated by CHAT_SOURCE_TWITTER_ENABLED +
    TWITTER_BEARER_TOKEN.
    """

    BASE_URL = "https://api.twitter.com/2/tweets/search/recent"

    def __init__(self, bearer_token: str, http_client: httpx.AsyncClient | None = None):
        self.bearer_token = bearer_token
        self._client = http_client

    async def fetch(self, stock: Stock) -> list[RawMessage]:
        if not self.bearer_token:
            return []

        query = f"{stock.symbol} lang:en -is:retweet"
        params = {
            "query": query,
            "max_results": "25",
            "tweet.fields": "created_at,author_id",
        }
        headers = {"Authorization": f"Bearer {self.bearer_token}"}

        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.get(self.BASE_URL, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
        finally:
            if owns_client:
                await client.aclose()

        messages: list[RawMessage] = []
        for tweet in data.get("data", []):
            messages.append(
                RawMessage(
                    text=tweet.get("text") or "",
                    author=str(tweet.get("author_id") or ""),
                    source="twitter",
                    posted_at=_parse_timestamp(tweet.get("created_at")),
                    url=f"https://twitter.com/i/web/status/{tweet.get('id')}" if tweet.get("id") else "",
                )
            )
        return messages


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)
