from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.core.models import Stock


class RedditChatSource(ChatSource):
    """Reddit's public read-only search JSON endpoint.

    Uses the unauthenticated `www.reddit.com/search.json` endpoint (works
    without OAuth for read access, but is more aggressively rate-limited
    than the authenticated `oauth.reddit.com` API). REDDIT_CLIENT_ID/SECRET
    are reserved in config for a future OAuth upgrade if the public endpoint
    proves too rate-limited in practice. Gated by CHAT_SOURCE_REDDIT_ENABLED.
    Reddit requires a descriptive User-Agent on every request.
    """

    BASE_URL = "https://www.reddit.com/search.json"
    USER_AGENT = "VibeTrading/0.1 (research chat source)"

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._client = http_client

    async def fetch(self, stock: Stock) -> list[RawMessage]:
        query = stock.name or stock.symbol
        params = {"q": query, "sort": "new", "limit": "25", "restrict_sr": "false"}
        headers = {"User-Agent": self.USER_AGENT}

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
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            title = post.get("title") or ""
            selftext = post.get("selftext") or ""
            messages.append(
                RawMessage(
                    text=f"{title}\n{selftext}".strip(),
                    author=post.get("author") or "",
                    source="reddit",
                    posted_at=_from_epoch(post.get("created_utc")),
                    url=f"https://reddit.com{post['permalink']}" if post.get("permalink") else "",
                )
            )
        return messages


def _from_epoch(value: float | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(value, tz=UTC)
