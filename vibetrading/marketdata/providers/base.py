from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from vibetrading.core.exceptions import MarketDataUnavailableError
from vibetrading.core.models import Candle, OptionChainSnapshot, Quote, Stock

# Yahoo addresses Indian instruments by ticker plus an exchange suffix, and
# indices by their own caret-prefixed codes. This is the whole "symbol
# mapping" the analysis path needs -- no per-stock ID to look up, register or
# keep in sync, which is the entire reason market data no longer goes through
# the broker.
_INDEX_SYMBOLS = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
    "FINNIFTY": "^CNXFIN",
    "SENSEX": "^BSESN",
}

_EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}


def yahoo_symbol(stock: Stock) -> str:
    """The Yahoo ticker for one instrument, derived from what the user
    already typed into their watchlist."""
    symbol = stock.symbol.upper().strip()
    if symbol in _INDEX_SYMBOLS:
        return _INDEX_SYMBOLS[symbol]
    if symbol.startswith("^") or "." in symbol:
        return symbol  # already a fully-qualified ticker
    return f"{symbol}{_EXCHANGE_SUFFIX.get(stock.exchange.upper(), '.NS')}"


def is_index(stock: Stock) -> bool:
    return stock.symbol.upper().strip() in _INDEX_SYMBOLS


class MarketDataProvider(ABC):
    """Where analysis gets its prices.

    Deliberately separate from BrokerClient. The broker is for trading --
    placing orders, reading positions and funds -- and addresses instruments
    by a broker-specific numeric security ID. Analysis only needs public
    market data, which free providers serve by ticker symbol, so requiring a
    Dhan security ID before a stock could be *looked at* was a configuration
    burden with no upside.
    """

    name: str = "unknown"

    @abstractmethod
    async def get_quote(self, stock: Stock) -> Quote:
        """Current price, OHLC and volume."""

    @abstractmethod
    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        """Daily candles covering the requested range."""

    async def get_option_chain(self, stock: Stock, strikes_around_atm: int = 5) -> OptionChainSnapshot:
        """Option-chain metrics around the money.

        Not abstract: most providers have no option data for Indian
        instruments. One that doesn't says so here, and the snapshot renders
        UNAVAILABLE -- it never invents open interest or implied volatility.
        """
        raise MarketDataUnavailableError(
            f"{self.name} does not provide option-chain data for {stock.symbol}."
        )

    async def aclose(self) -> None:
        """Release any connection pool. Safe to call more than once."""
