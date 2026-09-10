from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_db
from vibetrading.config import get_settings
from vibetrading.core.enums import AgentType
from vibetrading.persistence.repositories import (
    get_latest_agent_output,
    get_stock_by_symbol,
    list_signals_for_stock,
)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("")
async def get_watchlist(session: AsyncSession = Depends(get_db)) -> list[dict]:
    settings = get_settings()
    rows = []
    for symbol in settings.watchlist_symbols:
        stock = await get_stock_by_symbol(session, symbol)
        technical = await get_latest_agent_output(session, symbol, AgentType.TECHNICAL.value)
        research = await get_latest_agent_output(session, symbol, AgentType.RESEARCH.value)
        signals = await list_signals_for_stock(session, symbol)
        latest_signal = signals[0] if signals else None

        rows.append(
            {
                "symbol": symbol,
                "name": stock.name if stock else None,
                "technical_confidence": technical.confidence if technical else None,
                "research_confidence": research.confidence if research else None,
                "latest_signal": {
                    "action": latest_signal.action,
                    "confidence": latest_signal.confidence,
                    "timestamp": latest_signal.timestamp,
                }
                if latest_signal
                else None,
            }
        )
    return rows
