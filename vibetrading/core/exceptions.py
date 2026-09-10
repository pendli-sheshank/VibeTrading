from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibetrading.core.models import RiskCheckResult


class VibeTradingError(Exception):
    """Base class for all VibeTrading domain errors."""


class BrokerError(VibeTradingError):
    """Raised when a broker/Execution Agent call fails."""


class OrderRejectedError(BrokerError):
    """Raised when the broker rejects an order outright."""


class RiskRejectedError(VibeTradingError):
    """Raised when the Risk Agent declines to approve a signal.

    Carries the RiskCheckResult so callers can inspect exactly which
    rule(s) failed and why.
    """

    def __init__(self, message: str, result: "RiskCheckResult | None" = None):
        super().__init__(message)
        self.result = result


class InvalidRiskTokenError(VibeTradingError):
    """Raised when a BrokerClient receives a missing/invalid/expired/used RiskApprovalToken."""


class LLMError(VibeTradingError):
    """Raised when an LLM adapter call fails or returns an unusable response."""
