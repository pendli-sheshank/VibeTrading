from __future__ import annotations

import os
from collections.abc import Iterable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from vibetrading.config import get_settings
from vibetrading.persistence.orm_models import Base, UserORM
from vibetrading.rate_limit import limiter
from vibetrading.settings.cache import reset_tenant_settings_cache

# Every tenant_id a test hardcodes, across the whole suite -- kept in sync
# manually (see test_watchlist_seeding.py's cross-tenant test for the only
# user of id 2). A users row must exist for each, or FK-enforcing dialects
# (Postgres; SQLite doesn't enforce FKs by default) reject any tenant-scoped
# insert against it.
DEFAULT_TEST_TENANT_IDS = (1, 2)

# The fast unit/integration tier runs against an in-memory SQLite DB by
# default (no external service needed). Setting TEST_DATABASE_URL to a real
# Postgres DSN (see the CI Postgres-service job) switches every test that
# goes through make_test_engine()/db_session to it instead -- this is what
# catches dialect-specific issues (constraint-naming, batch-mode DDL, JSON
# handling, ...) before merge, per Phase 19's DoD.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite://")


def make_test_engine() -> AsyncEngine:
    """The one place every test that needs its own isolated engine (rather
    than the shared db_session fixture) should build it -- keeps SQLite's
    single-shared-connection quirks (StaticPool + check_same_thread=False,
    needed so multiple async tasks/sessions can share the one in-memory DB
    a bare "sqlite+aiosqlite://" URL implies) out of every individual test
    file, and means pointing TEST_DATABASE_URL at Postgres actually reaches
    every test, not just the ones using db_session."""
    if TEST_DATABASE_URL.startswith("sqlite"):
        return create_async_engine(
            TEST_DATABASE_URL, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    return create_async_engine(TEST_DATABASE_URL)


async def reset_schema(engine: AsyncEngine) -> None:
    """Drop-then-create every table -- a no-op on a fresh SQLite
    in-memory DB, but what makes a shared, persistent Postgres test DB
    (TEST_DATABASE_URL) behave like one too: each test starts from a
    clean schema instead of accumulating the previous test's rows."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def seed_test_users(engine: AsyncEngine, tenant_ids: Iterable[int] = DEFAULT_TEST_TENANT_IDS) -> None:
    """Inserts a placeholder UserORM row for each id so every tenant_id
    this suite hardcodes satisfies tenant-scoped tables' FK to `users` --
    needed for FK-enforcing dialects (Postgres) even though SQLite's
    default non-enforcement lets tests get away without it. Skip this for
    a fixture that needs a genuinely empty users table (test_auth.py's
    real register/login flow assigns its own ids)."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        for tenant_id in tenant_ids:
            session.add(UserORM(id=tenant_id, email=f"tenant{tenant_id}@test.local", hashed_password="x"))
        await session.commit()


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """An isolated session per test, tables created fresh -- SQLite
    in-memory by default, or TEST_DATABASE_URL's Postgres DB in CI's
    Postgres-integration job."""
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)

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
