from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.broker.base import BrokerClient
from vibetrading.core.models import Candle, FundsSnapshot, Stock
from vibetrading.llm.providers.mock_provider import MockLLMAdapter

BASE_DATE = datetime(2024, 1, 1, tzinfo=UTC)


def _candle(day: int, close: float, low: float | None = None, high: float | None = None) -> Candle:
    return Candle(
        timestamp=BASE_DATE + timedelta(days=day),
        open=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close,
        volume=100_000,
    )


class FixtureBroker(BrokerClient):
    """Returns a fixed, hand-crafted candle series regardless of the
    requested date range — gives the test full control to hand-verify
    exactly which trades the engine should produce.
    """

    def __init__(self, candles: list[Candle]):
        self._candles = candles

    async def get_historical_candles(self, stock, interval, from_date, to_date) -> list[Candle]:
        return self._candles

    async def place_order(self, order_request, risk_token):
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    async def get_positions(self):
        raise NotImplementedError

    async def get_funds(self) -> FundsSnapshot:
        raise NotImplementedError

    async def get_ltp(self, stock) -> float:
        raise NotImplementedError

    async def subscribe_market_feed(self, stocks, on_tick: Callable) -> None:
        raise NotImplementedError


def _build_fixture_candles() -> list[Candle]:
    # Days 0-18: flat filler so the engine has its 20-candle warmup by day 19.
    candles = [_candle(day, close=100.0) for day in range(19)]
    # Day 19: decision point 1 (BUY) -> long entry at 100.
    candles.append(_candle(19, close=100.0))
    # Days 20-21: decision points 2 & 3 (HOLD). Keep lows above the 3% stop
    # (97.0) so the position isn't stopped out early.
    candles.append(_candle(20, close=105.0, low=104.0, high=106.0))
    candles.append(_candle(21, close=110.0, low=109.0, high=111.0))
    # Day 22: decision point 4 (SELL) -> closes the long at 120.
    candles.append(_candle(22, close=120.0, low=119.0, high=121.0))
    # Day 23: decision point 5 (SELL, no open position) -> opens a short at 115.
    candles.append(_candle(23, close=115.0, low=114.0, high=116.0))
    # Day 24: decision point 6 (BUY) -> closes the short at 90.
    candles.append(_candle(24, close=90.0, low=89.0, high=91.0))
    return candles


@pytest.fixture
def fixture_candles() -> list[Candle]:
    return _build_fixture_candles()


def _make_strategy_agent() -> StrategyAgent:
    responses = [
        '{"action": "buy", "confidence": 0.9, "reasoning": "day19 buy", "stop_loss_pct": null}',
        '{"action": "hold", "confidence": 0.5, "reasoning": "day20 hold", "stop_loss_pct": null}',
        '{"action": "hold", "confidence": 0.5, "reasoning": "day21 hold", "stop_loss_pct": null}',
        '{"action": "sell", "confidence": 0.9, "reasoning": "day22 sell closes long", "stop_loss_pct": null}',
        '{"action": "sell", "confidence": 0.9, "reasoning": "day23 sell opens short", "stop_loss_pct": null}',
        '{"action": "buy", "confidence": 0.9, "reasoning": "day24 buy closes short", "stop_loss_pct": null}',
    ]
    llm = MockLLMAdapter(responses=responses)
    return StrategyAgent(llm=llm)


async def test_backtest_produces_hand_verifiable_trades(fixture_candles):
    broker = FixtureBroker(fixture_candles)
    engine = BacktestEngine(broker=broker, strategy_agent=_make_strategy_agent(), quantity=1)
    stock = Stock(symbol="TCS")

    result = await engine.run(stock, start_date=BASE_DATE, end_date=BASE_DATE + timedelta(days=24))

    assert result.total_trades == 2

    long_trade, short_trade = result.trades
    assert long_trade.direction == "long"
    assert long_trade.entry_price == pytest.approx(100.0)
    assert long_trade.exit_price == pytest.approx(120.0)
    assert long_trade.pnl == pytest.approx(20.0)

    assert short_trade.direction == "short"
    assert short_trade.entry_price == pytest.approx(115.0)
    assert short_trade.exit_price == pytest.approx(90.0)
    assert short_trade.pnl == pytest.approx(25.0)

    assert result.total_pnl == pytest.approx(45.0)
    assert result.win_rate == pytest.approx(1.0)


async def test_backtest_stop_loss_closes_losing_long_trade():
    candles = [_candle(day, close=100.0) for day in range(19)]
    candles.append(_candle(19, close=100.0))
    # Day 20: price crashes through the mandatory 3% stop (97.0) before any
    # SELL signal arrives -- the engine's own stop-loss check must close it,
    # not the LLM.
    candles.append(_candle(20, close=95.0, low=94.0, high=96.0))
    # A couple more HOLD-ish days so the loop has decision points to consume
    # without asserting anything further opens.
    candles.append(_candle(21, close=95.0, low=94.0, high=96.0))

    responses = [
        '{"action": "buy", "confidence": 0.9, "reasoning": "buy", "stop_loss_pct": null}',
        '{"action": "hold", "confidence": 0.5, "reasoning": "hold", "stop_loss_pct": null}',
        '{"action": "hold", "confidence": 0.5, "reasoning": "hold", "stop_loss_pct": null}',
    ]
    llm = MockLLMAdapter(responses=responses)
    strategy_agent = StrategyAgent(llm=llm)

    broker = FixtureBroker(candles)
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent, quantity=1)
    stock = Stock(symbol="INFY")

    result = await engine.run(stock, start_date=BASE_DATE, end_date=BASE_DATE + timedelta(days=21))

    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.direction == "long"
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(97.0)  # stop-loss price, not the close
    assert trade.pnl == pytest.approx(-3.0)


async def test_backtest_with_no_signals_produces_no_trades():
    candles = [_candle(day, close=100.0) for day in range(25)]
    llm = MockLLMAdapter(default_response='{"action": "hold", "confidence": 0.5, "reasoning": "x", "stop_loss_pct": null}')
    strategy_agent = StrategyAgent(llm=llm)

    broker = FixtureBroker(candles)
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent)
    stock = Stock(symbol="HDFC")

    result = await engine.run(stock, start_date=BASE_DATE, end_date=BASE_DATE + timedelta(days=24))

    assert result.total_trades == 0
    assert result.total_pnl == 0.0
    assert result.win_rate == 0.0
