from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.core.models import AgentOutput, Stock
from vibetrading.persistence.orm_models import AgentRunORM, StockORM


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
