from __future__ import annotations

from vibetrading.core.models import Stock
from vibetrading.orchestrator.watchlist import get_watchlist
from vibetrading.persistence.repositories import (
    DEFAULT_WATCHLIST_STOCKS,
    delete_stock,
    list_stocks,
    seed_default_watchlist_if_empty,
    upsert_stock,
)

TENANT_ID = 1


async def test_seed_writes_defaults_when_empty(db_session):
    await seed_default_watchlist_if_empty(db_session, TENANT_ID)
    await db_session.flush()

    rows = await list_stocks(db_session, TENANT_ID)
    assert {r.symbol for r in rows} == {s.symbol for s in DEFAULT_WATCHLIST_STOCKS}


async def test_seed_is_a_no_op_when_stocks_already_exist(db_session):
    await upsert_stock(db_session, TENANT_ID, Stock(symbol="WIPRO"))
    await db_session.flush()

    await seed_default_watchlist_if_empty(db_session, TENANT_ID)
    await db_session.flush()

    rows = await list_stocks(db_session, TENANT_ID)
    assert {r.symbol for r in rows} == {"WIPRO"}


async def test_get_watchlist_reflects_db_including_security_id(db_session):
    await upsert_stock(db_session, TENANT_ID, Stock(symbol="RELIANCE", dhan_security_id="2885"))
    await upsert_stock(db_session, TENANT_ID, Stock(symbol="TCS"))
    await db_session.flush()

    watchlist = await get_watchlist(db_session, TENANT_ID)

    by_symbol = {s.symbol: s for s in watchlist}
    assert by_symbol["RELIANCE"].dhan_security_id == "2885"
    assert by_symbol["TCS"].dhan_security_id is None


async def test_get_watchlist_empty_when_no_stocks(db_session):
    assert await get_watchlist(db_session, TENANT_ID) == []


async def test_delete_stock_removes_it_from_watchlist(db_session):
    await upsert_stock(db_session, TENANT_ID, Stock(symbol="HDFC"))
    await db_session.flush()

    removed = await delete_stock(db_session, TENANT_ID, "HDFC")
    await db_session.flush()

    assert removed is True
    assert await get_watchlist(db_session, TENANT_ID) == []


async def test_delete_missing_stock_returns_false(db_session):
    assert await delete_stock(db_session, TENANT_ID, "NOPE") is False


async def test_seeding_is_independent_per_tenant(db_session):
    await upsert_stock(db_session, 1, Stock(symbol="WIPRO"))
    await db_session.flush()

    await seed_default_watchlist_if_empty(db_session, 1)  # tenant 1 already has a stock -> no-op
    await seed_default_watchlist_if_empty(db_session, 2)  # tenant 2 is empty -> gets defaults
    await db_session.flush()

    assert {r.symbol for r in await list_stocks(db_session, 1)} == {"WIPRO"}
    assert {r.symbol for r in await list_stocks(db_session, 2)} == {s.symbol for s in DEFAULT_WATCHLIST_STOCKS}
