from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from vibetrading.broker.base import BrokerClient
from vibetrading.core.models import FundsSnapshot, Position
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.orchestrator.stop_loss_monitor import StopLossMonitor
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.engine import RiskEngine


class FakePositionsBroker(BrokerClient):
    """A broker double with fixed, controllable positions/LTP -- lets the
    test force a stop-loss breach deterministically without depending on
    MockBrokerClient's seeded price walk."""

    def __init__(self, positions: list[Position], ltp_by_symbol: dict[str, float]):
        self._positions = positions
        self._ltp = ltp_by_symbol
        self.place_order_calls: list[tuple] = []

    async def place_order(self, order_request, risk_token):
        self._require_valid_token(risk_token)
        self.place_order_calls.append((order_request, risk_token))
        from vibetrading.core.enums import OrderStatus
        from vibetrading.core.models import OrderResult

        return OrderResult(
            order_id="stop-loss-fill-1",
            status=OrderStatus.FILLED,
            filled_quantity=order_request.quantity,
            filled_price=self._ltp[order_request.stock_symbol],
            realized_pnl=0.0,
        )

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def get_positions(self) -> list[Position]:
        return self._positions

    async def get_funds(self) -> FundsSnapshot:
        return FundsSnapshot(available_balance=1_000_000.0)

    async def get_historical_candles(self, stock, interval, from_date, to_date):
        return []

    async def get_ltp(self, stock) -> float:
        return self._ltp[stock.symbol]

    async def subscribe_market_feed(self, stocks, on_tick: Callable) -> None:
        return None


def make_config(**overrides) -> RiskConfig:
    from vibetrading.core.enums import KillSwitchMode

    defaults = dict(
        max_position_size_inr=1_000_000,
        max_pct_capital_per_stock=1.0,
        max_concurrent_positions=10,
        max_daily_loss_inr=1_000_000,
        mandatory_stop_loss_pct=0.03,
        min_signal_confidence=0.65,
        max_total_exposure_pct=1.0,
        kill_switch_mode=KillSwitchMode.HALT_NEW_ORDERS,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


async def test_stop_loss_monitor_exits_breached_long_position(db_session):
    position = Position(
        stock_symbol="TCS",
        quantity=10,
        avg_price=100.0,
        stop_loss_price=95.0,
        opened_at=datetime.now(UTC),
    )
    broker = FakePositionsBroker(positions=[position], ltp_by_symbol={"TCS": 90.0})  # below stop
    risk_engine = RiskEngine(broker=broker, config=make_config())
    monitor = StopLossMonitor(broker=broker, risk_engine=risk_engine)

    queue = event_bus.subscribe()
    try:
        results = await monitor.check_all(db_session)

        assert len(results) == 1
        assert results[0].approved is True
        assert len(broker.place_order_calls) == 1
        order_request, _ = broker.place_order_calls[0]
        assert order_request.side.value == "sell"
        assert order_request.quantity == 10

        messages = []
        while not queue.empty():
            messages.append(queue.get_nowait())
        assert any(m["type"] == "stop_loss_exit" and m["approved"] for m in messages)
    finally:
        event_bus.unsubscribe(queue)


async def test_stop_loss_monitor_ignores_position_within_stop(db_session):
    position = Position(
        stock_symbol="TCS",
        quantity=10,
        avg_price=100.0,
        stop_loss_price=95.0,
        opened_at=datetime.now(UTC),
    )
    broker = FakePositionsBroker(positions=[position], ltp_by_symbol={"TCS": 98.0})  # above stop
    risk_engine = RiskEngine(broker=broker, config=make_config())
    monitor = StopLossMonitor(broker=broker, risk_engine=risk_engine)

    results = await monitor.check_all(db_session)

    assert results == []
    assert broker.place_order_calls == []


async def test_stop_loss_monitor_exits_breached_short_position(db_session):
    position = Position(
        stock_symbol="INFY",
        quantity=-10,
        avg_price=100.0,
        stop_loss_price=103.0,
        opened_at=datetime.now(UTC),
    )
    broker = FakePositionsBroker(positions=[position], ltp_by_symbol={"INFY": 105.0})  # above stop for a short
    risk_engine = RiskEngine(broker=broker, config=make_config())
    monitor = StopLossMonitor(broker=broker, risk_engine=risk_engine)

    results = await monitor.check_all(db_session)

    assert len(results) == 1
    order_request, _ = broker.place_order_calls[0]
    assert order_request.side.value == "buy"
    assert order_request.quantity == 10


async def test_stop_loss_monitor_ignores_position_without_stop_loss(db_session):
    position = Position(
        stock_symbol="TCS", quantity=10, avg_price=100.0, stop_loss_price=None, opened_at=datetime.now(UTC)
    )
    broker = FakePositionsBroker(positions=[position], ltp_by_symbol={"TCS": 1.0})
    risk_engine = RiskEngine(broker=broker, config=make_config())
    monitor = StopLossMonitor(broker=broker, risk_engine=risk_engine)

    results = await monitor.check_all(db_session)

    assert results == []
