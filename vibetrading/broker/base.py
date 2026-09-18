from __future__ import annotations

from abc import ABC, abstractmethod

from vibetrading.core.exceptions import InvalidRiskTokenError
from vibetrading.core.models import (
    FundsSnapshot,
    OrderRequest,
    OrderResult,
    Position,
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

    # No market-data methods here by design. Quotes, candles and option
    # chains come from vibetrading/marketdata/providers/, which addresses
    # instruments by ticker. Routing analysis through the broker meant every
    # stock needed a broker-specific security ID configured before it could
    # even be looked at -- a setup burden for data that is public and free.
    # The broker's job is trading: orders, positions, funds.
