from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

from vibetrading.core.exceptions import InvalidRiskTokenError, MarketDataUnavailableError
from vibetrading.core.models import (
    Candle,
    FundsSnapshot,
    OptionChainSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    Stock,
)
from vibetrading.risk.tokens import RiskApprovalToken, verify_and_consume_token


class BrokerClient(ABC):
    """The Execution Agent contract.

    Every broker integration (Dhan first, Zerodha/Upstox/etc. later) implements
    this same interface, so orchestration code and the Risk Agent never depend
    on a specific broker's SDK. `MockBrokerClient` implements it too, for
    local dev, tests, and paper-trading mode.
    """

    @staticmethod
    def _require_valid_token(risk_token: RiskApprovalToken | None) -> None:
        """Every place_order() implementation MUST call this first. This is
        what makes RiskEngine.approve_and_execute the only path to a real
        order — a missing, forged, expired, or reused token is refused
        before any broker/network call happens.
        """
        if not verify_and_consume_token(risk_token):
            raise InvalidRiskTokenError(
                "place_order called without a valid, unused RiskApprovalToken. "
                "Orders must go through risk.engine.RiskEngine.approve_and_execute()."
            )

    @abstractmethod
    async def place_order(self, order_request: OrderRequest, risk_token: RiskApprovalToken) -> OrderResult:
        """Submit an order. Never call this directly — go through
        risk.engine.RiskEngine.approve_and_execute(), the only path that can
        produce a valid risk_token. Implementations must call
        self._require_valid_token(risk_token) before doing anything else."""

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

    async def get_quote(self, stock: Stock) -> Quote:
        """Current price/OHLC/volume for one instrument.

        Not abstract: a broker integration that can't serve quotes says so
        here, and the market-data layer renders UNAVAILABLE for it. It must
        never synthesize one from whatever else it has -- a made-up price
        would feed straight into a real trading decision.
        """
        raise MarketDataUnavailableError(
            f"{type(self).__name__} does not support quote lookups for {stock.symbol}."
        )

    async def get_option_chain(self, stock: Stock, strikes_around_atm: int = 5) -> OptionChainSnapshot:
        """Option-chain metrics (OI, IV, volume) around the money.

        Same contract as get_quote: unsupported means raise, never invent.
        """
        raise MarketDataUnavailableError(
            f"{type(self).__name__} does not support option chains for {stock.symbol}."
        )

    @abstractmethod
    async def subscribe_market_feed(
        self, stocks: list[Stock], on_tick: Callable[[str, float], None]
    ) -> None:
        """Register a callback invoked with (symbol, last_traded_price) on each tick."""
