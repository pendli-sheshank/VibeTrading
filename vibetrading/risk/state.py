from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.config import get_settings
from vibetrading.core.enums import KillSwitchMode
from vibetrading.persistence.orm_models import RiskStateORM


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


async def get_or_create_risk_state(session: AsyncSession, tenant_id: int) -> RiskStateORM:
    state = await session.get(RiskStateORM, tenant_id)
    if state is None:
        settings = get_settings()
        state = RiskStateORM(
            tenant_id=tenant_id,
            kill_switch_active=settings.vibetrading_kill_switch,
            kill_switch_mode=settings.vibetrading_kill_switch_mode.value,
            daily_realized_pnl=0.0,
            daily_pnl_date=_today_iso(),
            updated_at=datetime.now(UTC),
        )
        session.add(state)
        await session.flush()

    _reset_if_new_day(state)
    return state


def _reset_if_new_day(state: RiskStateORM) -> None:
    today = _today_iso()
    if state.daily_pnl_date != today:
        state.daily_realized_pnl = 0.0
        state.daily_pnl_date = today


async def record_realized_pnl(session: AsyncSession, tenant_id: int, pnl: float) -> RiskStateORM:
    state = await get_or_create_risk_state(session, tenant_id)
    state.daily_realized_pnl += pnl
    state.updated_at = datetime.now(UTC)
    return state


async def set_kill_switch(
    session: AsyncSession, tenant_id: int, active: bool, mode: KillSwitchMode | None = None, reason: str | None = None
) -> RiskStateORM:
    state = await get_or_create_risk_state(session, tenant_id)
    state.kill_switch_active = active
    if mode is not None:
        state.kill_switch_mode = mode.value
    state.reason = reason
    state.updated_at = datetime.now(UTC)
    return state
