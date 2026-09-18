from __future__ import annotations

import logging

from vibetrading.config import Settings
from vibetrading.marketdata.providers.base import MarketDataProvider, is_index, yahoo_symbol
from vibetrading.marketdata.providers.live import LiveMarketDataProvider
from vibetrading.marketdata.providers.nse import NseOptionChainSource
from vibetrading.marketdata.providers.simulated import SimulatedMarketDataProvider
from vibetrading.marketdata.providers.yahoo import YahooMarketDataProvider

logger = logging.getLogger(__name__)

__all__ = [
    "LiveMarketDataProvider",
    "MarketDataProvider",
    "NseOptionChainSource",
    "SimulatedMarketDataProvider",
    "YahooMarketDataProvider",
    "close_market_data_providers",
    "get_market_data_provider",
    "is_index",
    "yahoo_symbol",
]


_PROVIDERS: dict[str, MarketDataProvider] = {}


def get_market_data_provider(settings: Settings) -> MarketDataProvider:
    """The analysis data source for one tenant.

    Live by default and with no configuration at all: the free providers
    need no API key and address instruments by ticker. "simulated" exists
    for offline development and tests, and is always labelled as such.

    One instance per kind, reused process-wide. Providers hold an
    httpx.AsyncClient, which is built for concurrent reuse and pools
    connections; constructing one per request instead would open a fresh
    pool on every page load and never close it.
    """
    kind = settings.market_data_provider
    provider = _PROVIDERS.get(kind)
    if provider is None:
        if kind == "simulated":
            logger.info("Market data: simulated provider selected; prices are synthetic, not real.")
            provider = SimulatedMarketDataProvider()
        else:
            provider = LiveMarketDataProvider()
        _PROVIDERS[kind] = provider
    return provider


async def close_market_data_providers() -> None:
    """Release every pooled connection. Called from the app's lifespan."""
    for provider in list(_PROVIDERS.values()):
        await provider.aclose()
    _PROVIDERS.clear()
