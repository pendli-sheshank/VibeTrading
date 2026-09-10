from __future__ import annotations

from vibetrading.core.models import Stock
from vibetrading.persistence.repositories import get_stock_by_symbol, list_stocks, upsert_stock

TENANT_ID = 1


async def test_upsert_and_read_stock(db_session):
    stock = Stock(symbol="reliance", exchange="NSE", name="Reliance Industries")

    await upsert_stock(db_session, TENANT_ID, stock)
    await db_session.commit()

    fetched = await get_stock_by_symbol(db_session, TENANT_ID, "RELIANCE")
    assert fetched is not None
    assert fetched.symbol == "RELIANCE"
    assert fetched.name == "Reliance Industries"

    all_stocks = await list_stocks(db_session, TENANT_ID)
    assert len(all_stocks) == 1


async def test_upsert_is_idempotent_on_symbol(db_session):
    await upsert_stock(db_session, TENANT_ID, Stock(symbol="TCS", name="Old Name"))
    await db_session.commit()

    await upsert_stock(db_session, TENANT_ID, Stock(symbol="TCS", name="New Name"))
    await db_session.commit()

    all_stocks = await list_stocks(db_session, TENANT_ID)
    assert len(all_stocks) == 1
    assert all_stocks[0].name == "New Name"


async def test_stocks_are_isolated_per_tenant(db_session):
    await upsert_stock(db_session, 1, Stock(symbol="TCS", name="Tenant 1's TCS"))
    await upsert_stock(db_session, 2, Stock(symbol="TCS", name="Tenant 2's TCS"))
    await db_session.commit()

    tenant_1_stocks = await list_stocks(db_session, 1)
    tenant_2_stocks = await list_stocks(db_session, 2)
    assert [s.name for s in tenant_1_stocks] == ["Tenant 1's TCS"]
    assert [s.name for s in tenant_2_stocks] == ["Tenant 2's TCS"]
