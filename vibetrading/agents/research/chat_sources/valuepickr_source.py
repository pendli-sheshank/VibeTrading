from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.core.models import Stock


class ValuePickrChatSource(ChatSource):
    """ValuePickr (forum.valuepickr.com) is a Discourse forum; Discourse
    instances commonly expose a public `/search.json?q=` endpoint. This uses
    that endpoint on a best-effort basis. Confirm ValuePickr's terms of
    service permit this kind of automated access before enabling it in
    production — gated by CHAT_SOURCE_VALUEPICKR_ENABLED, off by default.
    """

    BASE_URL = "https://forum.valuepickr.com/search.json"

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._client = http_client

    async def fetch(self, stock: Stock) -> list[RawMessage]:
        query = stock.name or stock.symbol
        params = {"q": query}

        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        finally:
            if owns_client:
                await client.aclose()

        messages: list[RawMessage] = []
        for post in data.get("posts", []):
            messages.append(
                RawMessage(
                    text=post.get("blurb") or "",
                    author=post.get("username") or "",
                    source="valuepickr",
                    posted_at=_parse_timestamp(post.get("created_at")),
                    url=(
                        f"https://forum.valuepickr.com/t/{post.get('topic_id')}/{post.get('post_number')}"
                        if post.get("topic_id")
                        else ""
                    ),
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
