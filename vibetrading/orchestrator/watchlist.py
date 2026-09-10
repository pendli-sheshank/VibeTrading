from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.core.models import Stock
from vibetrading.persistence.repositories import list_stocks


async def get_watchlist(session: AsyncSession) -> list[Stock]:
    """The configured stock universe — DB-backed (the `stocks` table),
    editable via the Settings UI, including each stock's Dhan security ID
    (required before DhanBrokerClient can trade it live). A fresh install's
    table is seeded with defaults by
    persistence.repositories.seed_default_watchlist_if_empty(), called once
    from the app's lifespan.
    """
    rows = await list_stocks(session)
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
