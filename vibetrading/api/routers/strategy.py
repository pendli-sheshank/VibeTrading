from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.on_demand import analyze_stock
from vibetrading.agents.strategy.performance_tracker import get_performance_summary
from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.marketdata import build_market_snapshot
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import (
    get_latest_agent_output,
    get_stock_by_symbol,
    list_signals_for_stock,
)
from vibetrading.settings.cache import get_tenant_settings

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


@router.get("/{symbol}/market-data")
async def get_market_data(
    symbol: str,
    session: AsyncSession = Depends(get_db),
    broker: BrokerClient = Depends(get_broker),
    user: UserORM = Depends(current_active_user),
) -> dict:
    """Live market data for one watchlist stock, with per-section status.

    Always 200 with a status field rather than an error status code for
    missing data: "this instrument has no option chain" is a fact about the
    instrument, not a failed request.
    """
    stock = await _watchlist_stock(session, user.id, symbol)
    snapshot = await build_market_snapshot(broker, stock)
    return snapshot.model_dump(mode="json")


@router.post("/{symbol}/analyze")
async def analyze(
    symbol: str,
    session: AsyncSession = Depends(get_db),
    broker: BrokerClient = Depends(get_broker),
    user: UserORM = Depends(current_active_user),
) -> dict:
    """Run both agents for one stock now and return the structured result.

    The JSON counterpart of the dashboard's Analyze button: same code path,
    same DATA_INSUFFICIENT semantics, no HTML.
    """
    stock = await _watchlist_stock(session, user.id, symbol)
    result = await analyze_stock(
        broker=broker,
        settings=get_tenant_settings(user.id),
        stock=stock,
        session=session,
        tenant_id=user.id,
    )
    await session.commit()
    return result.to_dict()


async def _watchlist_stock(session: AsyncSession, tenant_id: int, symbol: str) -> Stock:
    stock_orm = await get_stock_by_symbol(session, tenant_id, symbol.upper())
    if stock_orm is None:
        raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not on your watchlist.")
    return Stock(
        symbol=stock_orm.symbol,
        exchange=stock_orm.exchange,
        dhan_security_id=stock_orm.dhan_security_id,
        name=stock_orm.name,
        sector=stock_orm.sector,
    )


@router.get("/{symbol}")
async def get_strategy_detail(
    symbol: str, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> dict:
    symbol = symbol.upper()
    technical = await get_latest_agent_output(session, user.id, symbol, AgentType.TECHNICAL.value)
    research = await get_latest_agent_output(session, user.id, symbol, AgentType.RESEARCH.value)
    signals = await list_signals_for_stock(session, user.id, symbol)
    performance = await get_performance_summary(session, user.id, symbol)

    return {
        "symbol": symbol,
        "technical": _agent_output_dict(technical),
        "research": _agent_output_dict(research),
        "signals": [_signal_dict(s) for s in signals[:20]],
        "performance": performance,
    }
