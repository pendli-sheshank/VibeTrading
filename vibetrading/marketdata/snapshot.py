from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from vibetrading.agents.strategy.technical_indicators import (
    MIN_CANDLES_FOR_ANALYSIS,
    candles_to_dataframe,
    compute_all_indicators,
)
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import DataStatus, MarketDirection
from vibetrading.core.exceptions import BrokerError, MarketDataUnavailableError
from vibetrading.core.models import MarketSnapshot, OptionChainSnapshot, Quote, Stock
from vibetrading.core.reliability import CircuitBreakerOpenError

logger = logging.getLogger(__name__)

# CircuitBreakerOpenError does not descend from BrokerError, so handling only
# BrokerError left it to escape as an unhandled 500 -- and it fires exactly
# when the broker has already failed repeatedly, i.e. when the user most
# needs the screen to explain itself.
DATA_FETCH_ERRORS = (BrokerError, CircuitBreakerOpenError)

# How old the newest candle may be before the technical read is labelled
# DELAYED rather than LIVE. Generous enough to cover a normal weekend plus a
# public holiday without crying stale on a Tuesday morning.
FRESHNESS_WINDOW = timedelta(days=4)

DEFAULT_LOOKBACK_DAYS = 150


async def build_market_snapshot(
    broker: BrokerClient, stock: Stock, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> MarketSnapshot:
    """Collect everything an analysis would run on, before running one.

    Every section is fetched independently and carries its own status, so a
    missing option chain never blanks out a perfectly good technical read
    (and vice versa). Nothing here invents a value: a section that can't be
    fetched is reported UNAVAILABLE/ERROR with the provider's own message.
    """
    now = datetime.now(UTC)
    messages: list[str] = []

    quote = await _fetch_quote(broker, stock, messages)
    candles, candle_error = await _fetch_candles(broker, stock, lookback_days, now)
    if candle_error:
        messages.append(candle_error)

    indicators: dict = {}
    indicator_status = DataStatus.DATA_INSUFFICIENT
    indicator_message = candle_error
    direction = MarketDirection.UNKNOWN

    if candles:
        if len(candles) < MIN_CANDLES_FOR_ANALYSIS:
            indicator_message = (
                f"Only {len(candles)} daily candle(s) available; {MIN_CANDLES_FOR_ANALYSIS} are needed "
                "before indicators can be computed."
            )
            messages.append(indicator_message)
        else:
            indicators = compute_all_indicators(candles_to_dataframe(candles))
            newest = max(c.timestamp for c in candles)
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=UTC)
            if quote.status == DataStatus.SIMULATED:
                indicator_status = DataStatus.SIMULATED
            elif now - newest > FRESHNESS_WINDOW:
                indicator_status = DataStatus.DELAYED
                messages.append(f"Most recent candle is from {newest:%Y-%m-%d}, older than the freshness window.")
            else:
                indicator_status = DataStatus.LIVE
            direction = read_direction(indicators)
    elif candle_error is None:
        indicator_message = "No historical candles were returned for this instrument."
        messages.append(indicator_message)

    option_chain = await _fetch_option_chain(broker, stock)

    return MarketSnapshot(
        symbol=stock.symbol,
        quote=quote,
        indicators=indicators,
        indicator_status=indicator_status,
        indicator_message=indicator_message,
        candle_count=len(candles),
        direction=direction,
        option_chain=option_chain,
        generated_at=now,
        source=type(broker).__name__,
        # One root cause (an unmapped security ID, say) fails several
        # sections at once and reports the same sentence each time; showing
        # it once reads as an explanation, three times reads as a stutter.
        messages=list(dict.fromkeys(messages)),
    )


async def _fetch_quote(broker: BrokerClient, stock: Stock, messages: list[str]) -> Quote:
    try:
        return await broker.get_quote(stock)
    except MarketDataUnavailableError as exc:
        messages.append(str(exc))
        return Quote(symbol=stock.symbol, status=DataStatus.UNAVAILABLE, source=type(broker).__name__, message=str(exc))
    except DATA_FETCH_ERRORS as exc:
        logger.warning("Quote fetch failed for %s: %s", stock.symbol, exc)
        messages.append(str(exc))
        return Quote(symbol=stock.symbol, status=DataStatus.ERROR, source=type(broker).__name__, message=str(exc))


async def _fetch_candles(
    broker: BrokerClient, stock: Stock, lookback_days: int, now: datetime
) -> tuple[list, str | None]:
    try:
        candles = await broker.get_historical_candles(stock, "1d", now - timedelta(days=lookback_days), now)
        return sorted(candles, key=lambda c: c.timestamp), None
    except MarketDataUnavailableError as exc:
        return [], str(exc)
    except DATA_FETCH_ERRORS as exc:
        logger.warning("Candle fetch failed for %s: %s", stock.symbol, exc)
        return [], str(exc)


async def _fetch_option_chain(broker: BrokerClient, stock: Stock) -> OptionChainSnapshot:
    try:
        return await broker.get_option_chain(stock)
    except MarketDataUnavailableError as exc:
        return OptionChainSnapshot(
            symbol=stock.symbol, status=DataStatus.UNAVAILABLE, source=type(broker).__name__, message=str(exc)
        )
    except DATA_FETCH_ERRORS as exc:
        logger.warning("Option chain fetch failed for %s: %s", stock.symbol, exc)
        return OptionChainSnapshot(
            symbol=stock.symbol, status=DataStatus.ERROR, source=type(broker).__name__, message=str(exc)
        )


def read_direction(indicators: dict) -> MarketDirection:
    """Net directional read from the computed indicators.

    Weighs trend (price vs SMA-20/50), momentum (MACD histogram) and
    mean-reversion (RSI extremes) as one vote each, then takes the majority.
    Returns UNKNOWN -- never a coin-flip NEUTRAL -- when nothing is known.
    """
    close = indicators.get("close")
    if close is None:
        return MarketDirection.UNKNOWN

    bullish = 0
    bearish = 0

    sma_20 = indicators.get("sma_20")
    if sma_20 is not None:
        bullish += close > sma_20
        bearish += close < sma_20

    sma_50 = indicators.get("sma_50")
    if sma_50 is not None:
        bullish += close > sma_50
        bearish += close < sma_50

    macd_hist = indicators.get("macd_histogram")
    if macd_hist is not None:
        bullish += macd_hist > 0
        bearish += macd_hist < 0

    rsi = indicators.get("rsi_14")
    if rsi is not None:
        bullish += rsi <= 30  # oversold -> mean-reversion upward
        bearish += rsi >= 70

    if bullish == bearish == 0:
        return MarketDirection.UNKNOWN
    if bullish > bearish:
        return MarketDirection.BULLISH
    if bearish > bullish:
        return MarketDirection.BEARISH
    return MarketDirection.NEUTRAL
