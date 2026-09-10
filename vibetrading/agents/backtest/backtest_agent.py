from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.core.models import BacktestResult, Stock
from vibetrading.persistence.repositories import save_backtest_run


class BacktestAgent:
    """Thin persistence wrapper around BacktestEngine — what the dashboard's
    Backtest tab and scripts/run_backtest.py actually call. Kept separate
    from BacktestEngine so the engine itself has no DB dependency and stays
    trivially unit-testable.
    """

    def __init__(self, engine: BacktestEngine):
        self.engine = engine

    async def run_and_persist(
        self,
        session: AsyncSession,
        tenant_id: int,
        stock: Stock,
        start_date: datetime,
        end_date: datetime,
        warmup_days: int = 90,
    ) -> BacktestResult:
        result = await self.engine.run(stock, start_date, end_date, warmup_days=warmup_days)
        await save_backtest_run(session, tenant_id, result)
        return result
