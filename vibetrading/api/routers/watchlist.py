from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.core.enums import AgentType
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import (
    get_latest_agent_output,
    list_signals_for_stock,
    list_stocks,
)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("")
async def get_watchlist(
    session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> list[dict]:
    stocks = await list_stocks(session, user.id)
    rows = []
    for stock in stocks:
        symbol = stock.symbol
        technical = await get_latest_agent_output(session, user.id, symbol, AgentType.TECHNICAL.value)
        research = await get_latest_agent_output(session, user.id, symbol, AgentType.RESEARCH.value)
        signals = await list_signals_for_stock(session, user.id, symbol)
        latest_signal = signals[0] if signals else None

        rows.append(
            {
                "symbol": symbol,
                "name": stock.name,
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
