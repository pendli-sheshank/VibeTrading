from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.core.models import Stock
from vibetrading.persistence.repositories import list_stocks


async def get_watchlist(session: AsyncSession, tenant_id: int) -> list[Stock]:
    """A tenant's configured stock universe — DB-backed (the `stocks`
    table), editable via the Settings UI, including each stock's Dhan
    security ID (required before DhanBrokerClient can trade it live). A
    freshly-registered tenant's table is seeded with defaults by
    persistence.repositories.seed_default_watchlist_if_empty().
    """
    rows = await list_stocks(session, tenant_id)
    return [
        Stock(
            symbol=row.symbol,
            exchange=row.exchange,
            dhan_security_id=row.dhan_security_id,
            name=row.name,
            sector=row.sector,
        )
        for row in rows
    ]
