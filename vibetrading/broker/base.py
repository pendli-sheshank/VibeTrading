from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

from vibetrading.core.models import (
    Candle,
    FundsSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Stock,
)


class BrokerClient(ABC):
    """The Execution Agent contract.

    Every broker integration (Dhan first, Zerodha/Upstox/etc. later) implements
    this same interface, so orchestration code and the Risk Agent never depend
    on a specific broker's SDK. `MockBrokerClient` implements it too, for
    local dev, tests, and paper-trading mode.
    """

    @abstractmethod
    async def place_order(self, order_request: OrderRequest) -> OrderResult:
        """Submit an order. Callers outside the Risk Agent must never call this
        directly in live mode — see risk.engine.RiskEngine.approve_and_execute,
        which is the only sanctioned path to real order placement."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    async def get_funds(self) -> FundsSnapshot:
        ...

    @abstractmethod
    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        ...

    @abstractmethod
    async def get_ltp(self, stock: Stock) -> float:
        """Last traded price."""

    @abstractmethod
    async def subscribe_market_feed(
        self, stocks: list[Stock], on_tick: Callable[[str, float], None]
    ) -> None:
        """Register a callback invoked with (symbol, last_traded_price) on each tick."""
