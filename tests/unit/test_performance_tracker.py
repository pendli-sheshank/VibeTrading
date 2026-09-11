from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vibetrading.agents.strategy.performance_tracker import (
    get_performance_summary,
    reconcile_signal_performance,
)
from vibetrading.core.enums import ActionType, SignalSource
from vibetrading.core.models import Signal
from vibetrading.persistence.repositories import save_signal


def make_signal(action: ActionType, reference_price: float = 100.0, quantity: int = 10) -> Signal:
    return Signal(
        stock_symbol="INFY",
        timestamp=datetime.now(UTC),
        source=SignalSource.STRATEGY_AGENT,
        action=action,
        confidence=0.7,
        reasoning="test",
        suggested_quantity=quantity,
        reference_price=reference_price,
    )


async def test_reconcile_buy_signal_profit(db_session):
    orm_signal = await save_signal(db_session, 1, make_signal(ActionType.BUY, reference_price=100.0))
    await db_session.flush()

    await reconcile_signal_performance(db_session, orm_signal, current_price=110.0)

    assert orm_signal.realized_pnl == pytest.approx(100.0)  # (110-100) * 10
    assert orm_signal.realized_at is not None


async def test_reconcile_sell_signal_profit_on_price_drop(db_session):
    orm_signal = await save_signal(db_session, 1, make_signal(ActionType.SELL, reference_price=100.0))
    await db_session.flush()

    await reconcile_signal_performance(db_session, orm_signal, current_price=90.0)

    assert orm_signal.realized_pnl == pytest.approx(100.0)  # (100-90) * 10


async def test_reconcile_hold_signal_is_neutral(db_session):
    orm_signal = await save_signal(db_session, 1, make_signal(ActionType.HOLD, reference_price=100.0))
    await db_session.flush()

    await reconcile_signal_performance(db_session, orm_signal, current_price=150.0)

    assert orm_signal.realized_pnl == 0.0


async def test_performance_summary_win_rate(db_session):
    win = await save_signal(db_session, 1, make_signal(ActionType.BUY, reference_price=100.0))
    loss = await save_signal(db_session, 1, make_signal(ActionType.BUY, reference_price=100.0))
    await db_session.flush()

    await reconcile_signal_performance(db_session, win, current_price=120.0)
    await reconcile_signal_performance(db_session, loss, current_price=90.0)
    await db_session.commit()

    summary = await get_performance_summary(db_session, 1, "INFY")
    assert summary["total_signals"] == 2
    assert summary["win_rate"] == pytest.approx(0.5)
    assert summary["total_pnl"] == pytest.approx((20.0 * 10) + (-10.0 * 10))


async def test_performance_summary_empty_when_no_signals(db_session):
    summary = await get_performance_summary(db_session, 1, "NOSUCHSTOCK")
    assert summary["total_signals"] == 0
    assert summary["win_rate"] is None
