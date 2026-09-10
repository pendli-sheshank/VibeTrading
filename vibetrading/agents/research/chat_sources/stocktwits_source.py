from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.core.models import Stock


class StockTwitsChatSource(ChatSource):
    """StockTwits' public per-symbol stream endpoint. Read-only and does not
    require authentication, but is rate-limited — keep polling intervals
    reasonable. Gated by CHAT_SOURCE_STOCKTWITS_ENABLED.
    """

    BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol"

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._client = http_client

    async def fetch(self, stock: Stock) -> list[RawMessage]:
        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.get(f"{self.BASE_URL}/{stock.symbol}.json")
            response.raise_for_status()
            data = response.json()
        finally:
            if owns_client:
                await client.aclose()

        messages: list[RawMessage] = []
        for msg in data.get("messages", []):
            messages.append(
                RawMessage(
                    text=msg.get("body") or "",
                    author=(msg.get("user") or {}).get("username") or "",
                    source="stocktwits",
                    posted_at=_parse_timestamp(msg.get("created_at")),
                    url=f"https://stocktwits.com/message/{msg.get('id')}" if msg.get("id") else "",
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
