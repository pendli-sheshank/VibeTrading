from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.strategy.performance_tracker import get_performance_summary
from vibetrading.api.deps import get_db
from vibetrading.core.enums import AgentType
from vibetrading.persistence.repositories import get_latest_agent_output, list_signals_for_stock

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


def _agent_output_dict(output) -> dict | None:
    if output is None:
        return None
    return {
        "timestamp": output.timestamp,
        "confidence": output.confidence,
        "summary": output.summary,
        "raw_data": output.raw_data,
    }


def _signal_dict(signal) -> dict:
    return {
        "timestamp": signal.timestamp,
        "action": signal.action,
        "confidence": signal.confidence,
        "reasoning": signal.reasoning,
        "reference_price": signal.reference_price,
        "suggested_stop_loss": signal.suggested_stop_loss,
        "realized_pnl": signal.realized_pnl,
    }


@router.get("/{symbol}")
async def get_strategy_detail(symbol: str, session: AsyncSession = Depends(get_db)) -> dict:
    symbol = symbol.upper()
    technical = await get_latest_agent_output(session, symbol, AgentType.TECHNICAL.value)
    research = await get_latest_agent_output(session, symbol, AgentType.RESEARCH.value)
    signals = await list_signals_for_stock(session, symbol)
    performance = await get_performance_summary(session, symbol)

    return {
        "symbol": symbol,
        "technical": _agent_output_dict(technical),
        "research": _agent_output_dict(research),
        "signals": [_signal_dict(s) for s in signals[:20]],
        "performance": performance,
    }
