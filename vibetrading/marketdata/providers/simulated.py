from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from vibetrading.core.enums import DataStatus
from vibetrading.core.exceptions import MarketDataUnavailableError
from vibetrading.core.models import Candle, Quote, Stock
from vibetrading.marketdata.providers.base import MarketDataProvider


class SimulatedMarketDataProvider(MarketDataProvider):
    """Deterministic synthetic prices, for tests and offline development.

    Every value it returns is labelled SIMULATED and never LIVE, so a
    synthetic series can't be mistaken for the market on screen or by any
    consumer downstream.
    """

    name = "simulated"

    def __init__(self, seed: int = 42):
        self._seed = seed

    def _rng_for(self, symbol: str) -> random.Random:
        return random.Random(f"{self._seed}:{symbol}")

    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        rng = self._rng_for(stock.symbol)
        price = 100.0 + (rng.random() * 2000.0)
        step = timedelta(days=1) if interval in ("1d", "day", "daily") else timedelta(minutes=1)

        candles: list[Candle] = []
        current = from_date
        while current <= to_date:
            open_price = price
            close_price = max(0.05, open_price * (1 + rng.gauss(0, 0.012)))
            candles.append(
                Candle(
                    timestamp=current,
                    open=round(open_price, 2),
                    high=round(max(open_price, close_price) * (1 + abs(rng.gauss(0, 0.004))), 2),
                    low=round(min(open_price, close_price) * (1 - abs(rng.gauss(0, 0.004))), 2),
                    close=round(close_price, 2),
                    volume=rng.randint(50_000, 500_000),
                )
            )
            price = close_price
            current += step
        return candles

    async def get_quote(self, stock: Stock) -> Quote:
        now = datetime.now(UTC)
        candles = await self.get_historical_candles(stock, "1d", now - timedelta(days=5), now)
        if not candles:
            raise MarketDataUnavailableError(f"No simulated series available for {stock.symbol}.")

        latest = candles[-1]
        return Quote(
            symbol=stock.symbol,
            status=DataStatus.SIMULATED,
            last_price=latest.close,
            previous_close=candles[-2].close if len(candles) > 1 else None,
            open=latest.open,
            high=latest.high,
            low=latest.low,
            close=latest.close,
            volume=latest.volume,
            timestamp=latest.timestamp,
            source="simulated",
            message="Simulated data — not a real market price.",
        )

    async def get_option_chain(self, stock: Stock, strikes_around_atm: int = 5):
        raise MarketDataUnavailableError(
            f"Simulated market data does not include an option chain for {stock.symbol}. "
            "Fabricated open interest and implied volatility must never be shown as if they were real."
        )
