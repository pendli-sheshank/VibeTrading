from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vibetrading.config import get_settings
from vibetrading.persistence.orm_models import Base
from vibetrading.rate_limit import limiter
from vibetrading.settings.cache import reset_tenant_settings_cache


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


@pytest.fixture(autouse=True)
def _reset_tenant_settings():
    """settings/cache.py's per-tenant Settings cache is process-wide for the
    whole test session (mirrors the old single-tenant singleton's mutate-
    in-place design, just keyed by tenant_id now) -- clear it between every
    test so one test's saved settings for tenant N never leak into another
    test that reuses the same tenant_id."""
    yield
    reset_tenant_settings_cache()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """`limiter` (vibetrading/rate_limit.py) is one process-wide in-memory
    instance for the whole test session -- reset its counters before every
    test so an earlier test's requests to a rate-limited endpoint (e.g.
    /login, /register) never spuriously trip a later, unrelated test's
    limit."""
    limiter.reset()
    yield
