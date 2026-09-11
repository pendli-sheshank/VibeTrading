from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vibetrading.config import get_settings
from vibetrading.persistence.orm_models import Base


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    url = settings.database_url
    if url.startswith("postgresql"):
        # Pooling only makes sense against a real server -- SQLite (the
        # fast unit-test tier, and today's dev default) is a single local
        # file with no connection-count concept to bound.
        return create_async_engine(
            url,
            echo=False,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
        )
    return create_async_engine(url, echo=False)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def init_db() -> None:
    """Create all tables. Dev/test convenience; production should use Alembic migrations."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        yield session
