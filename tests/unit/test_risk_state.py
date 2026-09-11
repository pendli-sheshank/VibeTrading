from __future__ import annotations

import pytest

from vibetrading.core.enums import KillSwitchMode
from vibetrading.risk.state import get_or_create_risk_state, record_realized_pnl, set_kill_switch

TENANT_ID = 1


async def test_get_or_create_risk_state_creates_default_row(db_session):
    state = await get_or_create_risk_state(db_session, TENANT_ID)
    assert state.kill_switch_active is False
    assert state.daily_realized_pnl == 0.0


async def test_get_or_create_risk_state_is_idempotent(db_session):
    first = await get_or_create_risk_state(db_session, TENANT_ID)
    await db_session.flush()
    second = await get_or_create_risk_state(db_session, TENANT_ID)
    assert first.tenant_id == second.tenant_id


async def test_set_kill_switch_persists(db_session):
    await set_kill_switch(db_session, TENANT_ID, active=True, mode=KillSwitchMode.HALT_ALL, reason="manual stop")
    await db_session.flush()

    state = await get_or_create_risk_state(db_session, TENANT_ID)
    assert state.kill_switch_active is True
    assert state.kill_switch_mode == KillSwitchMode.HALT_ALL.value
    assert state.reason == "manual stop"


async def test_record_realized_pnl_accumulates(db_session):
    await record_realized_pnl(db_session, TENANT_ID, -100.0)
    await record_realized_pnl(db_session, TENANT_ID, 30.0)
    await db_session.flush()

    state = await get_or_create_risk_state(db_session, TENANT_ID)
    assert state.daily_realized_pnl == pytest.approx(-70.0)


async def test_daily_pnl_resets_on_new_day(db_session):
    state = await get_or_create_risk_state(db_session, TENANT_ID)
    state.daily_realized_pnl = -500.0
    state.daily_pnl_date = "2000-01-01"
    await db_session.flush()

    refreshed = await get_or_create_risk_state(db_session, TENANT_ID)
    assert refreshed.daily_realized_pnl == 0.0
    assert refreshed.daily_pnl_date != "2000-01-01"
