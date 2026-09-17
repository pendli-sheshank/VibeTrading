from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.api.deps import get_db, get_market_data
from vibetrading.auth.backend import current_active_user
from vibetrading.core.enums import AgentType
from vibetrading.core.exceptions import BrokerError, MarketDataUnavailableError
from vibetrading.core.models import Stock
from vibetrading.core.reliability import CircuitBreakerOpenError
from vibetrading.llm.router import LLMRouter
from vibetrading.marketdata.providers import MarketDataProvider
from vibetrading.persistence.orm_models import BacktestRunORM, UserORM
from vibetrading.persistence.repositories import (
    get_backtest_run,
    get_stock_by_symbol,
    list_backtest_runs_for_stock,
)
from vibetrading.settings.cache import get_tenant_settings

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRunRequest(BaseModel):
    """Validated up front so bad parameters are a 422 from FastAPI with a
    field-level message, never a 500 from somewhere deep in the engine."""

    symbol: str = Field(min_length=1, max_length=32)
    start_date: datetime | None = None
    end_date: datetime | None = None
    days: int = Field(default=180, ge=30, le=1825)
    warmup_days: int = Field(default=90, ge=0, le=365)
    quantity: int = Field(default=1, ge=1, le=100_000)


def _run_summary(run: BacktestRunORM) -> dict:
    return {
        "id": run.id,
        "stock_symbol": run.stock_symbol,
        "start_date": run.start_date,
        "end_date": run.end_date,
        "total_trades": run.total_trades,
        "win_rate": run.win_rate,
        "total_pnl": run.total_pnl,
        "max_drawdown": run.max_drawdown,
        "trades": run.trades,
        "created_at": run.created_at,
    }


@router.post("/run")
async def run_backtest(
    payload: BacktestRunRequest,
    session: AsyncSession = Depends(get_db),
    market_data: MarketDataProvider = Depends(get_market_data),
    user: UserORM = Depends(current_active_user),
) -> dict:
    end_date = payload.end_date or datetime.now(UTC)
    start_date = payload.start_date or (end_date - timedelta(days=payload.days))
    if start_date >= end_date:
        raise HTTPException(status_code=422, detail="start_date must be earlier than end_date.")

    symbol = payload.symbol.upper()
    stock_orm = await get_stock_by_symbol(session, user.id, symbol)
    if stock_orm is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not on your watchlist.")
    stock = Stock(
        symbol=stock_orm.symbol, exchange=stock_orm.exchange, dhan_security_id=stock_orm.dhan_security_id
    )

    llm = LLMRouter(get_tenant_settings(user.id)).get_adapter(AgentType.BACKTEST)
    strategy_agent = StrategyAgent(llm=llm)
    engine = BacktestEngine(market_data=market_data, strategy_agent=strategy_agent, quantity=payload.quantity)
    backtest_agent = BacktestAgent(engine=engine)

    try:
        result = await backtest_agent.run_and_persist(
            session, user.id, stock, start_date, end_date, warmup_days=payload.warmup_days
        )
    except MarketDataUnavailableError as exc:
        # Not a server fault: the provider has no data for this instrument or
        # range. 422 with the provider's own words beats a 500 with none.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CircuitBreakerOpenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await session.commit()
    return result.model_dump(mode="json")


@router.get("/{run_id}")
async def get_backtest(
    run_id: int, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> dict:
    run = await get_backtest_run(session, user.id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return _run_summary(run)


@router.get("")
async def list_backtests(
    symbol: str, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> list[dict]:
    runs = await list_backtest_runs_for_stock(session, user.id, symbol.upper())
    return [_run_summary(run) for run in runs]
