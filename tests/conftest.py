from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vibetrading.config import get_settings
from vibetrading.persistence.orm_models import Base


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """An isolated in-memory SQLite session per test, tables created fresh."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_global_settings():
    """get_settings() returns one process-wide singleton for the whole test
    session. Since settings/service.py mutates it in place (by design — see
    that module's docstring), any test that saves settings against the
    default singleton would otherwise leak state into every later test.
    Snapshot before, restore after, for every test automatically."""
    settings = get_settings()
    snapshot = settings.model_dump()
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)
