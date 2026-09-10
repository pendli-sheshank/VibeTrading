from __future__ import annotations

import pytest

from vibetrading.core.enums import KillSwitchMode
from vibetrading.risk.state import get_or_create_risk_state, record_realized_pnl, set_kill_switch


async def test_get_or_create_risk_state_creates_default_row(db_session):
    state = await get_or_create_risk_state(db_session)
    assert state.kill_switch_active is False
    assert state.daily_realized_pnl == 0.0


async def test_get_or_create_risk_state_is_idempotent(db_session):
    first = await get_or_create_risk_state(db_session)
    await db_session.flush()
    second = await get_or_create_risk_state(db_session)
    assert first.id == second.id


async def test_set_kill_switch_persists(db_session):
    await set_kill_switch(db_session, active=True, mode=KillSwitchMode.HALT_ALL, reason="manual stop")
    await db_session.flush()

    state = await get_or_create_risk_state(db_session)
    assert state.kill_switch_active is True
    assert state.kill_switch_mode == KillSwitchMode.HALT_ALL.value
    assert state.reason == "manual stop"


async def test_record_realized_pnl_accumulates(db_session):
    await record_realized_pnl(db_session, -100.0)
    await record_realized_pnl(db_session, 30.0)
    await db_session.flush()

    state = await get_or_create_risk_state(db_session)
    assert state.daily_realized_pnl == pytest.approx(-70.0)


async def test_daily_pnl_resets_on_new_day(db_session):
    state = await get_or_create_risk_state(db_session)
    state.daily_realized_pnl = -500.0
    state.daily_pnl_date = "2000-01-01"
    await db_session.flush()

    refreshed = await get_or_create_risk_state(db_session)
    assert refreshed.daily_realized_pnl == 0.0
    assert refreshed.daily_pnl_date != "2000-01-01"
