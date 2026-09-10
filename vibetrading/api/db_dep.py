from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.persistence.db import get_session

# Split out from api.deps (which every FastAPI route depends on) so that
# vibetrading.auth.manager can use the exact same override-able get_db
# dependency without creating an import cycle: api.deps -> auth.backend ->
# auth.manager -> api.deps. This module has no auth dependency at all, so
# both api.deps and auth.manager can import it freely, and
# app.dependency_overrides[get_db] (keyed on this one function object)
# applies to both the regular route tree and the auth routes alike.


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session
