from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_db
from vibetrading.config import get_settings
from vibetrading.persistence.repositories import list_stocks

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/mode")
async def get_mode(session: AsyncSession = Depends(get_db)) -> dict:
    settings = get_settings()
    stocks = await list_stocks(session)
    return {
        "execution_mode": settings.vibetrading_execution_mode.value,
        "has_dhan_credentials": settings.has_dhan_credentials,
        "watchlist": [s.symbol for s in stocks],
        "kill_switch_mode": settings.vibetrading_kill_switch_mode.value,
    }
