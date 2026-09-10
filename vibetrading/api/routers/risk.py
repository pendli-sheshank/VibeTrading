from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.core.enums import KillSwitchMode
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import list_recent_risk_events
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.state import get_or_create_risk_state, set_kill_switch
from vibetrading.settings.cache import get_tenant_settings

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/state")
async def get_risk_state(
    session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> dict:
    state = await get_or_create_risk_state(session, user.id)
    await session.commit()
    settings = get_tenant_settings(user.id)
    config = RiskConfig.from_settings(settings)

    return {
        "kill_switch_active": state.kill_switch_active,
        "kill_switch_mode": state.kill_switch_mode,
        "daily_realized_pnl": state.daily_realized_pnl,
        "daily_pnl_date": state.daily_pnl_date,
        "execution_mode": settings.vibetrading_execution_mode.value,
        "config": config.model_dump(mode="json"),
    }


class KillSwitchRequest(BaseModel):
    active: bool
    mode: KillSwitchMode | None = None
    reason: str | None = None


@router.post("/kill-switch")
async def set_kill_switch_endpoint(
    payload: KillSwitchRequest,
    session: AsyncSession = Depends(get_db),
    user: UserORM = Depends(current_active_user),
) -> dict:
    state = await set_kill_switch(session, user.id, active=payload.active, mode=payload.mode, reason=payload.reason)
    await session.commit()
    await event_bus.publish(
        {"type": "kill_switch", "active": state.kill_switch_active, "mode": state.kill_switch_mode},
        tenant_id=user.id,
    )
    return {"kill_switch_active": state.kill_switch_active, "kill_switch_mode": state.kill_switch_mode}


@router.get("/events")
async def get_risk_events(
    limit: int = 50, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> list[dict]:
    events = await list_recent_risk_events(session, user.id, limit=limit)
    return [
        {
            "stock_symbol": event.stock_symbol,
            "rule_name": event.rule_name,
            "passed": event.passed,
            "reason": event.reason,
            "timestamp": event.timestamp,
        }
        for event in events
    ]
