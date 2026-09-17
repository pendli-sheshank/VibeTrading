from __future__ import annotations

from datetime import datetime

import httpx

from vibetrading.core.models import Candle, OptionChainSnapshot, Quote, Stock
from vibetrading.marketdata.providers.base import MarketDataProvider
from vibetrading.marketdata.providers.nse import NseOptionChainSource
from vibetrading.marketdata.providers.yahoo import YahooMarketDataProvider


class LiveMarketDataProvider(MarketDataProvider):
    """The default analysis data source: Yahoo for prices, NSE for options.

    Two providers because no single free, key-less source covers both Indian
    equity prices and NSE option chains. They fail independently -- NSE being
    blocked (its usual answer to a server IP) costs the option-chain panel
    only, never the quote or the indicators.
    """

    name = "live"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._prices = YahooMarketDataProvider(client=client)
        self._options = NseOptionChainSource(client=client)

    async def get_quote(self, stock: Stock) -> Quote:
        return await self._prices.get_quote(stock)

    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        return await self._prices.get_historical_candles(stock, interval, from_date, to_date)

    async def get_option_chain(self, stock: Stock, strikes_around_atm: int = 5) -> OptionChainSnapshot:
        return await self._options.get_option_chain(stock, strikes_around_atm)

    async def aclose(self) -> None:
        await self._prices.aclose()
        await self._options.aclose()
