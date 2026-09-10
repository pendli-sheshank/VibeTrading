from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import list_stocks
from vibetrading.settings.cache import get_tenant_settings

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/mode")
async def get_mode(
    session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> dict:
    settings = get_tenant_settings(user.id)
    stocks = await list_stocks(session, user.id)
    return {
        "execution_mode": settings.vibetrading_execution_mode.value,
        "has_dhan_credentials": settings.has_dhan_credentials,
        "watchlist": [s.symbol for s in stocks],
        "kill_switch_mode": settings.vibetrading_kill_switch_mode.value,
    }
