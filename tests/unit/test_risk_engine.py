from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from vibetrading.broker.base import BrokerClient
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.enums import ActionType, KillSwitchMode, OrderStatus, SignalSource
from vibetrading.core.exceptions import InvalidRiskTokenError
from vibetrading.core.models import FundsSnapshot, OrderRequest, OrderResult, Signal, Stock
from vibetrading.persistence.orm_models import AuditLogORM, OrderORM
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.engine import RiskEngine
from vibetrading.risk.state import get_or_create_risk_state, record_realized_pnl, set_kill_switch

STOCK = Stock(symbol="TCS")


def make_config(**overrides) -> RiskConfig:
    defaults = dict(
        max_position_size_inr=50_000,
        max_pct_capital_per_stock=0.50,
        max_concurrent_positions=5,
        max_daily_loss_inr=10_000,
        mandatory_stop_loss_pct=0.03,
        min_signal_confidence=0.65,
        max_total_exposure_pct=0.90,
        kill_switch_mode=KillSwitchMode.HALT_NEW_ORDERS,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def make_signal(**overrides) -> Signal:
    defaults = dict(
        stock_symbol="TCS",
        timestamp=datetime.now(UTC),
        source=SignalSource.STRATEGY_AGENT,
        action=ActionType.BUY,
        confidence=0.8,
        reasoning="test",
        reference_price=100.0,
    )
    defaults.update(overrides)
    return Signal(**defaults)


class SpyBroker(BrokerClient):
    """Records whether it was called, and whether the audit row already
    existed (with status 'pending') at the moment place_order ran — proving
    the audit log is written before any broker call.
    """

    def __init__(self, session):
        self.session = session
        self.called = False
        self.audit_status_at_call: str | None = None

    async def place_order(self, order_request: OrderRequest, risk_token) -> OrderResult:
        self._require_valid_token(risk_token)
        self.called = True
        result = await self.session.execute(select(AuditLogORM))
        rows = result.scalars().all()
        self.audit_status_at_call = rows[0].status if rows else None
        return OrderResult(
            order_id="spy-order-1",
            status=OrderStatus.FILLED,
            filled_quantity=order_request.quantity,
            filled_price=100.0,
            realized_pnl=0.0,
        )

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def get_positions(self):
        return []

    async def get_funds(self) -> FundsSnapshot:
        return FundsSnapshot(available_balance=1_000_000.0)

    async def get_historical_candles(self, stock, interval, from_date, to_date):
        return []

    async def get_ltp(self, stock) -> float:
        return 100.0

    async def subscribe_market_feed(self, stocks, on_tick) -> None:
        return None


async def test_audit_log_written_before_broker_call(db_session):
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert broker.called is True
    assert broker.audit_status_at_call == "pending"
    assert result.approved is True
    assert result.order_result is not None


async def test_rejected_signal_never_reaches_broker(db_session):
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, config=make_config(min_signal_confidence=0.99))

    result = await engine.approve_and_execute(db_session, make_signal(confidence=0.5), STOCK)

    assert broker.called is False
    assert result.approved is False
    assert result.order_result is None
    assert result.risk_check.rule_results["min_confidence"] is False


async def test_hold_signal_skips_broker_and_is_approved_trivially(db_session):
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(action=ActionType.HOLD), STOCK)

    assert broker.called is False
    assert result.approved is True
    assert result.order_result is None


async def test_kill_switch_blocks_new_order_end_to_end(db_session):
    await set_kill_switch(db_session, active=True, mode=KillSwitchMode.HALT_NEW_ORDERS)
    await db_session.flush()

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert broker.called is False
    assert result.approved is False
    assert result.risk_check.rule_results["kill_switch"] is False


async def test_kill_switch_halt_new_orders_still_allows_stop_loss_exit(db_session):
    await set_kill_switch(db_session, active=True, mode=KillSwitchMode.HALT_NEW_ORDERS)
    await db_session.flush()

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, config=make_config())

    exit_signal = make_signal(source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL, confidence=0.0)
    result = await engine.approve_and_execute(db_session, exit_signal, STOCK)

    assert broker.called is True
    assert result.approved is True


async def test_daily_loss_circuit_breaker_trips_after_realized_loss(db_session):
    broker = MockBrokerClient(seed=1, initial_funds=1_000_000.0)
    engine = RiskEngine(broker=broker, config=make_config(max_daily_loss_inr=100.0))

    # Open a long position...
    buy_signal = make_signal(action=ActionType.BUY, reference_price=100.0, suggested_quantity=10)
    buy_result = await engine.approve_and_execute(db_session, buy_signal, STOCK)
    assert buy_result.approved and buy_result.order_result is not None

    # ...then close it at a big enough loss to trip the breaker (broker fills
    # at its own synthetic LTP, so force a large loss via a big quantity/price
    # assumption isn't reliable — instead directly record a large loss to
    # deterministically exercise the breaker-persists behavior).
    await record_realized_pnl(db_session, -150.0)
    await db_session.flush()

    blocked_signal = make_signal(action=ActionType.BUY, reference_price=100.0)
    blocked_result = await engine.approve_and_execute(db_session, blocked_signal, STOCK)

    assert blocked_result.approved is False
    assert blocked_result.risk_check.rule_results["daily_loss_circuit_breaker"] is False


async def test_realized_pnl_from_order_result_updates_risk_state(db_session):
    broker = SpyBrokerWithLoss(db_session)
    engine = RiskEngine(broker=broker, config=make_config())

    await engine.approve_and_execute(db_session, make_signal(), STOCK)
    await db_session.flush()

    state = await get_or_create_risk_state(db_session)
    assert state.daily_realized_pnl == pytest.approx(-500.0)


class SpyBrokerWithLoss(SpyBroker):
    async def place_order(self, order_request: OrderRequest, risk_token) -> OrderResult:
        self._require_valid_token(risk_token)
        self.called = True
        return OrderResult(
            order_id="spy-loss-1",
            status=OrderStatus.FILLED,
            filled_quantity=order_request.quantity,
            filled_price=100.0,
            realized_pnl=-500.0,
        )


async def test_successful_order_persists_order_row(db_session):
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)
    await db_session.flush()

    rows = (await db_session.execute(select(OrderORM))).scalars().all()
    assert len(rows) == 1
    assert rows[0].order_id == result.order_result.order_id
    assert rows[0].stock_symbol == "TCS"
    assert rows[0].status == "filled"


async def test_place_order_rejects_missing_token():
    broker = MockBrokerClient()
    order = OrderRequest(stock_symbol="TCS", side="buy", quantity=1, mode="paper")
    with pytest.raises(InvalidRiskTokenError):
        await broker.place_order(order, None)
