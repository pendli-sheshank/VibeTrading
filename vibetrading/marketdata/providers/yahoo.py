from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from vibetrading.core.enums import DataStatus
from vibetrading.core.exceptions import MarketDataError, MarketDataUnavailableError
from vibetrading.core.models import Candle, Quote, Stock
from vibetrading.core.reliability import with_retry_and_circuit_breaker
from vibetrading.marketdata.providers.base import MarketDataProvider, yahoo_symbol

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo serves anonymous requests but rejects obviously scripted ones.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

# Yahoo's `range` tokens ("6mo", "1y") are always anchored to today, so they
# cannot express a historical window. A backtest over 2020 asked with
# range=1y would quietly come back with the LAST year's candles instead --
# the right shape, the wrong period, and no error. Exact epoch bounds
# (period1/period2) are used everywhere a specific window is meant.


class YahooMarketDataProvider(MarketDataProvider):
    """Live quotes and daily candles from Yahoo Finance.

    Free and key-less, addressed by ticker (RELIANCE.NS, ^NSEI), which is why
    analysis no longer needs a broker security ID. One endpoint returns both
    the current quote and the candle series, so quotes and history come from
    the same source and can't disagree with each other.
    """

    name = "yahoo"

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 15.0):
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=_HEADERS, follow_redirects=True)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @with_retry_and_circuit_breaker(
        "marketdata:yahoo", retry_on=(MarketDataError,), not_a_failure=(MarketDataUnavailableError,)
    )
    async def _chart(self, stock: Stock, params: dict[str, str]) -> dict:
        symbol = yahoo_symbol(stock)
        params = {"interval": "1d", "includePrePost": "false", **params}

        try:
            response = await self._client.get(CHART_URL.format(symbol=symbol), params=params)
        except httpx.HTTPError as exc:
            raise MarketDataError(f"Yahoo Finance request failed for {symbol}: {exc}") from exc

        if response.status_code == 404:
            # Permanent for this ticker: a typo'd or unlisted symbol. Not
            # retried, and never counted against the circuit breaker.
            raise MarketDataUnavailableError(
                f"Yahoo Finance has no instrument called {symbol}. Check the symbol and exchange "
                f"for {stock.symbol} under Settings -> Watchlist."
            )
        if response.status_code != 200:
            raise MarketDataError(f"Yahoo Finance returned HTTP {response.status_code} for {symbol}.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError(f"Yahoo Finance returned a non-JSON body for {symbol}.") from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise MarketDataUnavailableError(f"Yahoo Finance rejected {symbol}: {chart['error']}")

        results = chart.get("result") or []
        if not results or not isinstance(results[0], dict):
            raise MarketDataUnavailableError(f"Yahoo Finance returned no data for {symbol}.")
        return results[0]

    async def get_quote(self, stock: Stock) -> Quote:
        # A short window: the quote comes from the payload meta, which is
        # the live tick regardless of how many candles were requested.
        result = await self._chart(stock, {"range": "5d"})
        meta = result.get("meta") or {}

        last_price = _as_float(meta.get("regularMarketPrice"))
        if last_price is None:
            raise MarketDataUnavailableError(
                f"Yahoo Finance returned no traded price for {stock.symbol}."
            )

        market_time = _as_int(meta.get("regularMarketTime"))
        timestamp = datetime.fromtimestamp(market_time, tz=UTC) if market_time else datetime.now(UTC)

        return Quote(
            symbol=stock.symbol,
            status=DataStatus.LIVE,
            last_price=last_price,
            previous_close=_as_float(meta.get("chartPreviousClose") or meta.get("previousClose")),
            open=_as_float(meta.get("regularMarketOpen")) or _latest_open(result),
            high=_as_float(meta.get("regularMarketDayHigh")),
            low=_as_float(meta.get("regularMarketDayLow")),
            close=last_price,
            volume=_as_int(meta.get("regularMarketVolume")),
            timestamp=timestamp,
            source=f"yahoo:{yahoo_symbol(stock)}",
        )

    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        result = await self._chart(
            stock,
            {"period1": str(int(from_date.timestamp())), "period2": str(int(to_date.timestamp()))},
        )

        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        series = quotes[0] if isinstance(quotes[0], dict) else {}

        opens, highs = series.get("open") or [], series.get("high") or []
        lows, closes = series.get("low") or [], series.get("close") or []
        volumes = series.get("volume") or []

        candles: list[Candle] = []
        for i, raw_ts in enumerate(timestamps):
            close = _as_float(_at(closes, i))
            if close is None:
                # Yahoo pads holidays and halted sessions with nulls. Skipping
                # them keeps the series honest; filling them would invent bars
                # that never traded.
                continue
            candles.append(
                Candle(
                    timestamp=datetime.fromtimestamp(float(raw_ts), tz=UTC),
                    open=_as_float(_at(opens, i)) or close,
                    high=_as_float(_at(highs, i)) or close,
                    low=_as_float(_at(lows, i)) or close,
                    close=close,
                    volume=_as_int(_at(volumes, i)) or 0,
                )
            )

        if not candles:
            raise MarketDataUnavailableError(
                f"Yahoo Finance returned no usable candles for {stock.symbol} "
                f"({from_date:%Y-%m-%d}..{to_date:%Y-%m-%d}). The window may cover only "
                "non-trading days, or predate the instrument's listing."
            )
        return candles


def _at(values: list, index: int):
    return values[index] if index < len(values) else None


def _latest_open(result: dict) -> float | None:
    series = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = [v for v in (series.get("open") or []) if v is not None]
    return _as_float(opens[-1]) if opens else None


def _as_float(value) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
