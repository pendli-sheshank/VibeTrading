from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.llm.router import LLMRouter
from vibetrading.persistence.orm_models import BacktestRunORM, UserORM
from vibetrading.persistence.repositories import get_backtest_run, list_backtest_runs_for_stock
from vibetrading.settings.cache import get_tenant_settings

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRunRequest(BaseModel):
    symbol: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    days: int = 180
    warmup_days: int = 90
    quantity: int = 1


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
    broker: BrokerClient = Depends(get_broker),
    user: UserORM = Depends(current_active_user),
) -> dict:
    end_date = payload.end_date or datetime.now(UTC)
    start_date = payload.start_date or (end_date - timedelta(days=payload.days))

    llm = LLMRouter(get_tenant_settings(user.id)).get_adapter(AgentType.BACKTEST)
    strategy_agent = StrategyAgent(llm=llm)
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent, quantity=payload.quantity)
    backtest_agent = BacktestAgent(engine=engine)

    stock = Stock(symbol=payload.symbol.upper())
    result = await backtest_agent.run_and_persist(
        session, user.id, stock, start_date, end_date, warmup_days=payload.warmup_days
    )
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
