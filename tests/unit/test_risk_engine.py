from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from vibetrading.broker.base import BrokerClient
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.enums import ActionType, KillSwitchMode, OrderStatus, SignalSource
from vibetrading.core.exceptions import InvalidRiskTokenError
from vibetrading.core.models import FundsSnapshot, OrderRequest, OrderResult, Signal, Stock
from vibetrading.orchestrator.lease import acquire_or_renew_lease
from vibetrading.persistence.orm_models import (
    AuditLogORM,
    OrderORM,
    TenantLeaseORM,
    UsedRiskTokenORM,
)
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.engine import RiskEngine
from vibetrading.risk.state import get_or_create_risk_state, record_realized_pnl, set_kill_switch
from vibetrading.risk.tokens import mint_token

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
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert broker.called is True
    assert broker.audit_status_at_call == "pending"
    assert result.approved is True
    assert result.order_result is not None


async def test_rejected_signal_never_reaches_broker(db_session):
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config(min_signal_confidence=0.99))

    result = await engine.approve_and_execute(db_session, make_signal(confidence=0.5), STOCK)

    assert broker.called is False
    assert result.approved is False
    assert result.order_result is None
    assert result.risk_check.rule_results["min_confidence"] is False


async def test_hold_signal_skips_broker_and_is_approved_trivially(db_session):
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(action=ActionType.HOLD), STOCK)

    assert broker.called is False
    assert result.approved is True
    assert result.order_result is None


async def test_kill_switch_blocks_new_order_end_to_end(db_session):
    await set_kill_switch(db_session, 1, active=True, mode=KillSwitchMode.HALT_NEW_ORDERS)
    await db_session.flush()

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert broker.called is False
    assert result.approved is False
    assert result.risk_check.rule_results["kill_switch"] is False


async def test_kill_switch_halt_new_orders_still_allows_stop_loss_exit(db_session):
    await set_kill_switch(db_session, 1, active=True, mode=KillSwitchMode.HALT_NEW_ORDERS)
    await db_session.flush()

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    exit_signal = make_signal(
        source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL, confidence=0.0, suggested_quantity=10
    )
    result = await engine.approve_and_execute(db_session, exit_signal, STOCK)

    assert broker.called is True
    assert result.approved is True


async def test_daily_loss_circuit_breaker_trips_after_realized_loss(db_session):
    broker = MockBrokerClient(seed=1, initial_funds=1_000_000.0)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config(max_daily_loss_inr=100.0))

    # Open a long position...
    buy_signal = make_signal(action=ActionType.BUY, reference_price=100.0, suggested_quantity=10)
    buy_result = await engine.approve_and_execute(db_session, buy_signal, STOCK)
    assert buy_result.approved and buy_result.order_result is not None

    # ...then close it at a big enough loss to trip the breaker (broker fills
    # at its own synthetic LTP, so force a large loss via a big quantity/price
    # assumption isn't reliable — instead directly record a large loss to
    # deterministically exercise the breaker-persists behavior).
    await record_realized_pnl(db_session, 1, -150.0)
    await db_session.flush()

    blocked_signal = make_signal(action=ActionType.BUY, reference_price=100.0)
    blocked_result = await engine.approve_and_execute(db_session, blocked_signal, STOCK)

    assert blocked_result.approved is False
    assert blocked_result.risk_check.rule_results["daily_loss_circuit_breaker"] is False


async def test_realized_pnl_from_order_result_updates_risk_state(db_session):
    broker = SpyBrokerWithLoss(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    await engine.approve_and_execute(db_session, make_signal(), STOCK)
    await db_session.flush()

    state = await get_or_create_risk_state(db_session, 1)
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
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

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


class ExplodingBroker(SpyBroker):
    """A broker whose place_order always fails after verifying the token —
    used to exercise RiskEngine's audit-log-on-failure path."""

    async def place_order(self, order_request: OrderRequest, risk_token) -> OrderResult:
        self._require_valid_token(risk_token)
        raise ConnectionError("broker unreachable")


async def test_broker_failure_marks_audit_entry_failed_and_propagates(db_session):
    broker = ExplodingBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    with pytest.raises(ConnectionError):
        await engine.approve_and_execute(db_session, make_signal(), STOCK)

    rows = (await db_session.execute(select(AuditLogORM))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "failed"

    order_rows = (await db_session.execute(select(OrderORM))).scalars().all()
    assert order_rows == []


async def test_no_fencing_token_means_no_distributed_enforcement(db_session):
    """The default (fencing_token=None) construction path -- every direct/
    manual/test use of RiskEngine before and after Phase 20 -- must behave
    exactly as before: no lease exists, no lease check runs, nothing about
    approval changes."""
    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert result.approved is True
    assert broker.called is True


async def test_a_matching_fencing_token_is_approved(db_session):
    fencing_token = await acquire_or_renew_lease(db_session, 1, "worker-a")
    await db_session.commit()

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config(), fencing_token=fencing_token)

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert result.approved is True
    assert broker.called is True


async def test_a_stale_fencing_token_is_rejected_before_touching_the_broker(db_session):
    """The core distributed-safety proof: a worker whose fencing_token has
    been superseded by another worker's takeover must never mint a token
    or reach the broker, regardless of what the risk rules would otherwise
    decide."""
    stale_token = await acquire_or_renew_lease(db_session, 1, "worker-a")
    await db_session.commit()

    # worker-a's lease lapses; worker-b takes over, bumping the fencing token.
    lease = await db_session.get(TenantLeaseORM, 1)
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await acquire_or_renew_lease(db_session, 1, "worker-b")
    await db_session.commit()

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config(), fencing_token=stale_token)

    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert result.approved is False
    assert result.risk_check.rule_results == {"lease_fencing": False}
    assert broker.called is False

    # Nothing was written -- not even an audit row -- for a cycle that
    # never had a verified lease to act under.
    assert (await db_session.execute(select(AuditLogORM))).scalars().all() == []


async def test_a_replayed_token_id_is_rejected_before_touching_the_broker(db_session, monkeypatch):
    """Defense-in-depth: even if a caller somehow got a previously-consumed
    token_id back into approve_and_execute()'s path (not achievable via the
    public API, since mint_token() always generates a fresh random id --
    this proves the durable used_risk_tokens guard itself works)."""
    token = mint_token(signal_id=None, stock_symbol=STOCK.symbol, quantity=1)
    db_session.add(UsedRiskTokenORM(token_id=token.token_id, tenant_id=1, consumed_at=datetime.now(UTC)))
    await db_session.commit()

    monkeypatch.setattr("vibetrading.risk.engine.mint_token", lambda **kwargs: token)

    broker = SpyBroker(db_session)
    engine = RiskEngine(broker=broker, tenant_id=1, config=make_config())
    result = await engine.approve_and_execute(db_session, make_signal(), STOCK)

    assert result.approved is False
    assert result.risk_check.rule_results == {"token_replay": False}
    assert broker.called is False
