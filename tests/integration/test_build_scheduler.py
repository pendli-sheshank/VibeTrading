from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings
from vibetrading.core.models import Stock
from vibetrading.orchestrator.scheduler import build_scheduler
from vibetrading.persistence.orm_models import Base
from vibetrading.persistence.repositories import upsert_stock


@pytest.fixture
async def test_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def patch_orchestrator_db_session(monkeypatch, test_engine):
    """build_scheduler()/orchestrator/watchlist.py resolve the watchlist via
    persistence.db.get_session(), which is normally bound to the app-wide
    (lru_cache'd) engine. Point every module that imports get_session at our
    isolated test engine instead, so this test never touches real app state.
    """
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def _test_get_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr("vibetrading.orchestrator.scheduler.get_session", _test_get_session)
    return session_factory


async def test_build_scheduler_resolves_watchlist_from_db_and_registers_jobs(
    test_engine, patch_orchestrator_db_session
):
    """Proves a DB-configured stock (with a Dhan security ID set) actually
    reaches APScheduler job registration -- this is what closes the
    original live-trading gap where no security ID was ever populated."""
    session_factory = patch_orchestrator_db_session
    async with session_factory() as session:
        await upsert_stock(session, 1, Stock(symbol="RELIANCE", dhan_security_id="2885"))
        await upsert_stock(session, 1, Stock(symbol="TCS"))
        await session.commit()

    broker = MockBrokerClient(seed=1)
    settings = Settings(_env_file=None)
    scheduler = await build_scheduler(broker, 1, settings)

    watchlist_symbols = {s.symbol for s in scheduler.watchlist}
    assert watchlist_symbols == {"RELIANCE", "TCS"}

    reliance = next(s for s in scheduler.watchlist if s.symbol == "RELIANCE")
    assert reliance.dhan_security_id == "2885"

    scheduler.start()
    try:
        job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
        assert "research-RELIANCE" in job_ids
        assert "strategy-RELIANCE" in job_ids
        assert "research-TCS" in job_ids
        assert "strategy-TCS" in job_ids
        assert "stop-loss-monitor" in job_ids
    finally:
        scheduler.shutdown()


async def test_build_scheduler_empty_watchlist_registers_no_stock_jobs(test_engine, patch_orchestrator_db_session):
    broker = MockBrokerClient(seed=1)
    scheduler = await build_scheduler(broker, 1, Settings(_env_file=None))

    assert scheduler.watchlist == []

    scheduler.start()
    try:
        job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
        assert job_ids == {"stop-loss-monitor"}
    finally:
        scheduler.shutdown()
