from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.core.models import Stock


class MockChatSource(ChatSource):
    """Deterministic canned chatter with a deliberately mixed sentiment mix.
    Always available, no network/keys required — the default source when no
    real chat source is enabled.
    """

    async def fetch(self, stock: Stock) -> list[RawMessage]:
        now = datetime.now(UTC)
        symbol = stock.symbol
        texts = [
            f"{symbol} looking strong today, breaking out of its recent range #bullish",
            f"Not convinced by {symbol} at these levels, booking some profits",
            f"{symbol} management sounded confident on the last earnings call",
            f"Volume on {symbol} has been picking up this week, worth watching",
            f"{symbol} facing resistance near recent highs, could see a pullback",
        ]
        return [
            RawMessage(text=text, author=f"mock_user_{i}", source="mock-chat", posted_at=now - timedelta(hours=i))
            for i, text in enumerate(texts)
        ]
