from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.core.models import AgentOutput, BacktestResult, Signal, Stock
from vibetrading.persistence.orm_models import (
    AgentRunORM,
    AuditLogORM,
    BacktestRunORM,
    OrderORM,
    RiskEventORM,
    SignalORM,
    StockORM,
)


async def upsert_stock(session: AsyncSession, stock: Stock) -> StockORM:
    result = await session.execute(select(StockORM).where(StockORM.symbol == stock.symbol))
    existing = result.scalar_one_or_none()
    if existing:
        existing.exchange = stock.exchange
        existing.dhan_security_id = stock.dhan_security_id
        existing.name = stock.name
        existing.sector = stock.sector
        return existing

    orm_stock = StockORM(
        symbol=stock.symbol,
        exchange=stock.exchange,
        dhan_security_id=stock.dhan_security_id,
        name=stock.name,
        sector=stock.sector,
    )
    session.add(orm_stock)
    return orm_stock


async def get_stock_by_symbol(session: AsyncSession, symbol: str) -> StockORM | None:
    result = await session.execute(select(StockORM).where(StockORM.symbol == symbol.upper()))
    return result.scalar_one_or_none()


async def list_stocks(session: AsyncSession) -> list[StockORM]:
    result = await session.execute(select(StockORM))
    return list(result.scalars().all())


async def save_agent_output(session: AsyncSession, output: AgentOutput) -> AgentRunORM:
    orm_output = AgentRunORM(
        agent_type=output.agent_type.value,
        stock_symbol=output.stock_symbol,
        timestamp=output.timestamp,
        confidence=output.confidence,
        summary=output.summary,
        raw_data=output.raw_data,
    )
    session.add(orm_output)
    return orm_output


async def get_latest_agent_output(
    session: AsyncSession, stock_symbol: str, agent_type: str
) -> AgentRunORM | None:
    result = await session.execute(
        select(AgentRunORM)
        .where(AgentRunORM.stock_symbol == stock_symbol, AgentRunORM.agent_type == agent_type)
        .order_by(AgentRunORM.timestamp.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def save_signal(session: AsyncSession, signal: Signal) -> SignalORM:
    orm_signal = SignalORM(
        stock_symbol=signal.stock_symbol,
        timestamp=signal.timestamp,
        source=signal.source.value,
        action=signal.action.value,
        confidence=signal.confidence,
        reasoning=signal.reasoning,
        contributing_output_ids=signal.contributing_output_ids,
        suggested_quantity=signal.suggested_quantity,
        suggested_stop_loss=signal.suggested_stop_loss,
        reference_price=signal.reference_price,
    )
    session.add(orm_signal)
    return orm_signal


async def get_signal(session: AsyncSession, signal_id: int) -> SignalORM | None:
    return await session.get(SignalORM, signal_id)


async def list_signals_for_stock(
    session: AsyncSession, stock_symbol: str, only_realized: bool = False
) -> list[SignalORM]:
    stmt = select(SignalORM).where(SignalORM.stock_symbol == stock_symbol)
    if only_realized:
        stmt = stmt.where(SignalORM.realized_pnl.is_not(None))
    stmt = stmt.order_by(SignalORM.timestamp.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def save_backtest_run(session: AsyncSession, result: BacktestResult) -> BacktestRunORM:
    orm_run = BacktestRunORM(
        stock_symbol=result.stock_symbol,
        start_date=result.start_date,
        end_date=result.end_date,
        total_trades=result.total_trades,
        win_rate=result.win_rate,
        total_pnl=result.total_pnl,
        max_drawdown=result.max_drawdown,
        trades=[trade.model_dump(mode="json") for trade in result.trades],
        created_at=datetime.now(UTC),
    )
    session.add(orm_run)
    return orm_run


async def get_backtest_run(session: AsyncSession, run_id: int) -> BacktestRunORM | None:
    return await session.get(BacktestRunORM, run_id)


async def list_backtest_runs_for_stock(session: AsyncSession, stock_symbol: str) -> list[BacktestRunORM]:
    stmt = (
        select(BacktestRunORM)
        .where(BacktestRunORM.stock_symbol == stock_symbol)
        .order_by(BacktestRunORM.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_recent_orders(session: AsyncSession, limit: int = 50) -> list[OrderORM]:
    stmt = select(OrderORM).order_by(OrderORM.timestamp.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_recent_audit_log(session: AsyncSession, limit: int = 50) -> list[AuditLogORM]:
    stmt = select(AuditLogORM).order_by(AuditLogORM.timestamp.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_recent_risk_events(session: AsyncSession, limit: int = 50) -> list[RiskEventORM]:
    stmt = select(RiskEventORM).order_by(RiskEventORM.timestamp.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
