from __future__ import annotations

from fastapi import APIRouter

from vibetrading.config import get_settings

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/mode")
async def get_mode() -> dict:
    settings = get_settings()
    return {
        "execution_mode": settings.vibetrading_execution_mode.value,
        "has_dhan_credentials": settings.has_dhan_credentials,
        "watchlist": settings.watchlist_symbols,
        "kill_switch_mode": settings.vibetrading_kill_switch_mode.value,
    }
