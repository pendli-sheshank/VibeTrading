from __future__ import annotations

import logging

from vibetrading.agents.research.chat_sources.base import ChatSource, RawMessage
from vibetrading.core.models import Stock

logger = logging.getLogger(__name__)


class TelegramChatSource(ChatSource):
    """Placeholder for Telegram public-channel chatter.

    Unlike the other sources, Telegram has no simple keyed REST search API
    for public channels/groups — a real integration needs an authenticated
    MTProto client (e.g. Telethon or Pyrogram) with a persisted login
    session, which can't be wired up non-interactively from an API key
    alone. TELEGRAM_API_ID/API_HASH are reserved in config for that future
    client. Until then this always returns an empty list; it stays gated
    behind CHAT_SOURCE_TELEGRAM_ENABLED like every other real source so
    enabling it is a deliberate, visible choice rather than a silent no-op.
    """

    def __init__(self, api_id: str = "", api_hash: str = ""):
        self.api_id = api_id
        self.api_hash = api_hash

    async def fetch(self, stock: Stock) -> list[RawMessage]:
        logger.warning(
            "TelegramChatSource.fetch(%s) called but no MTProto client is wired up yet; "
            "returning no messages. See module docstring.",
            stock.symbol,
        )
        return []
