from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel

from vibetrading.core.models import Stock


class RawMessage(BaseModel):
    text: str
    author: str = ""
    source: str
    posted_at: datetime
    url: str = ""


class ChatSource(ABC):
    """One social/forum chatter source (X/Twitter, Reddit, Telegram, StockTwits,
    ValuePickr-style forums). Every real source ships disabled by default —
    see .env.example's CHAT_SOURCE_*_ENABLED flags and the compliance note in
    the project plan — until the user confirms allowed use for that platform.
    """

    @abstractmethod
    async def fetch(self, stock: Stock) -> list[RawMessage]:
        ...
