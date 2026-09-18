from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vibetrading.core.enums import DataStatus, MarketDirection
from vibetrading.core.exceptions import BrokerError, MarketDataUnavailableError
from vibetrading.core.models import Candle, Stock
from vibetrading.core.reliability import CircuitBreakerOpenError
from vibetrading.marketdata import build_market_snapshot, read_direction
from vibetrading.marketdata.providers import SimulatedMarketDataProvider
from vibetrading.marketdata.providers.base import MarketDataProvider

STOCK = Stock(symbol="RELIANCE")


class StubProvider(MarketDataProvider):
    """Serves exactly the candles it is given, so a test can describe a data
    situation precisely. Quotes and option chains are unsupported unless a
    subclass overrides them."""

    name = "stub"

    def __init__(self, candles=None, candle_error=None):
        self._candles = candles or []
        self._candle_error = candle_error

    async def get_historical_candles(self, stock, interval, from_date, to_date):
        if self._candle_error:
            raise self._candle_error
        return self._candles

    async def get_quote(self, stock):
        raise MarketDataUnavailableError(f"StubProvider serves no quote for {stock.symbol}.")


def make_candles(count: int, *, end: datetime | None = None, start_price: float = 100.0) -> list[Candle]:
    end = end or datetime.now(UTC)
    candles = []
    price = start_price
    for i in range(count):
        price *= 1.01 if i % 3 else 0.995
        ts = end - timedelta(days=count - i - 1)
        candles.append(
            Candle(timestamp=ts, open=price * 0.99, high=price * 1.02, low=price * 0.98, close=price, volume=100_000)
        )
    return candles


async def test_simulated_provider_snapshot_is_labelled_simulated_never_live():
    """The single most important labelling rule: synthetic paper-trading
    prices must never be presented as real market data."""
    snapshot = await build_market_snapshot(SimulatedMarketDataProvider(seed=7), STOCK)

    assert snapshot.quote.status == DataStatus.SIMULATED
    assert snapshot.indicator_status == DataStatus.SIMULATED
    assert snapshot.is_sufficient_for_analysis is True
    assert "simulated" in (snapshot.quote.message or "").lower()


async def test_too_few_candles_is_data_insufficient_and_blocks_analysis():
    snapshot = await build_market_snapshot(StubProvider(candles=make_candles(10)), STOCK)

    assert snapshot.indicator_status == DataStatus.DATA_INSUFFICIENT
    assert snapshot.is_sufficient_for_analysis is False
    assert snapshot.indicators == {}
    assert any("10 daily candle" in m for m in snapshot.messages)


async def test_stale_candles_are_labelled_delayed_not_live():
    old_end = datetime.now(UTC) - timedelta(days=30)
    snapshot = await build_market_snapshot(StubProvider(candles=make_candles(80, end=old_end)), STOCK)

    assert snapshot.indicator_status == DataStatus.DELAYED
    assert any("older than the freshness window" in m for m in snapshot.messages)


async def test_fresh_candles_are_labelled_live_with_indicators_computed():
    snapshot = await build_market_snapshot(StubProvider(candles=make_candles(80)), STOCK)

    assert snapshot.indicator_status == DataStatus.LIVE
    assert snapshot.candle_count == 80
    assert snapshot.indicators["rsi_14"] is not None
    assert snapshot.indicators["atr_14"] is not None
    assert snapshot.direction != MarketDirection.UNKNOWN


async def test_provider_error_on_candles_is_reported_not_swallowed():
    broker = StubProvider(candle_error=BrokerError("Dhan rejected historical_daily_data: DH-905"))
    snapshot = await build_market_snapshot(broker, STOCK)

    assert snapshot.is_sufficient_for_analysis is False
    assert any("DH-905" in m for m in snapshot.messages)


async def test_unsupported_quote_and_option_chain_report_unavailable_without_inventing_values():
    snapshot = await build_market_snapshot(StubProvider(candles=make_candles(60)), STOCK)

    assert snapshot.quote.status == DataStatus.UNAVAILABLE
    assert snapshot.quote.last_price is None  # no invented price
    assert snapshot.option_chain.status == DataStatus.UNAVAILABLE
    assert snapshot.option_chain.strikes == []


async def test_simulated_provider_refuses_to_invent_an_option_chain():
    """Fabricated OI/IV could drive a real trade, so it is refused."""
    with pytest.raises(MarketDataUnavailableError, match="never be shown as if they were real"):
        await SimulatedMarketDataProvider().get_option_chain(STOCK)


def test_read_direction_is_unknown_without_data_rather_than_neutral():
    assert read_direction({}) == MarketDirection.UNKNOWN
    assert read_direction({"close": 100.0}) == MarketDirection.UNKNOWN


def test_read_direction_weighs_trend_and_momentum():
    bullish = read_direction(
        {"close": 110.0, "sma_20": 100.0, "sma_50": 95.0, "macd_histogram": 1.5, "rsi_14": 55.0}
    )
    bearish = read_direction(
        {"close": 90.0, "sma_20": 100.0, "sma_50": 105.0, "macd_histogram": -1.5, "rsi_14": 45.0}
    )

    assert bullish == MarketDirection.BULLISH
    assert bearish == MarketDirection.BEARISH


async def test_an_open_circuit_breaker_is_reported_not_raised():
    """CircuitBreakerOpenError isn't a BrokerError, so it used to escape
    every handler and surface as a 500 -- precisely when the broker was
    already failing and the screen most needed to explain itself."""

    class TrippedProvider(StubProvider):
        async def get_historical_candles(self, stock, interval, from_date, to_date):
            raise CircuitBreakerOpenError("Circuit 'dhan:123' is open")

        async def get_quote(self, stock):
            raise CircuitBreakerOpenError("Circuit 'dhan:123' is open")

    snapshot = await build_market_snapshot(TrippedProvider(), STOCK)

    assert snapshot.quote.status == DataStatus.ERROR
    assert snapshot.is_sufficient_for_analysis is False
    assert any("is open" in m for m in snapshot.messages)
