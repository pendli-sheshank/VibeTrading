from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

from vibetrading.core.exceptions import InvalidRiskTokenError
from vibetrading.core.models import (
    Candle,
    FundsSnapshot,
    OrderRequest,
    OrderResult,
    Position,
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

    @abstractmethod
    async def subscribe_market_feed(
        self, stocks: list[Stock], on_tick: Callable[[str, float], None]
    ) -> None:
        """Register a callback invoked with (symbol, last_traded_price) on each tick."""
