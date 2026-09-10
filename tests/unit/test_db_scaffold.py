from __future__ import annotations

from vibetrading.core.models import Stock
from vibetrading.persistence.repositories import get_stock_by_symbol, list_stocks, upsert_stock


async def test_upsert_and_read_stock(db_session):
    stock = Stock(symbol="reliance", exchange="NSE", name="Reliance Industries")

    await upsert_stock(db_session, stock)
    await db_session.commit()

    fetched = await get_stock_by_symbol(db_session, "RELIANCE")
    assert fetched is not None
    assert fetched.symbol == "RELIANCE"
    assert fetched.name == "Reliance Industries"

    all_stocks = await list_stocks(db_session)
    assert len(all_stocks) == 1


async def test_upsert_is_idempotent_on_symbol(db_session):
    await upsert_stock(db_session, Stock(symbol="TCS", name="Old Name"))
    await db_session.commit()

    await upsert_stock(db_session, Stock(symbol="TCS", name="New Name"))
    await db_session.commit()

    all_stocks = await list_stocks(db_session)
    assert len(all_stocks) == 1
    assert all_stocks[0].name == "New Name"
