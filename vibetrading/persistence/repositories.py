from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.core.models import Stock
from vibetrading.persistence.orm_models import StockORM


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
