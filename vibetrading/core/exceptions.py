from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibetrading.core.models import RiskCheckResult


class VibeTradingError(Exception):
    """Base class for all VibeTrading domain errors."""


class BrokerError(VibeTradingError):
    """Raised when a broker/Execution Agent call fails."""


class OrderRejectedError(BrokerError):
    """Raised when the broker rejects an order outright."""


class MarketDataUnavailableError(BrokerError):
    """Raised when market data genuinely cannot be obtained — the instrument
    isn't mapped to a broker security ID, the broker doesn't support that
    data type at all, or the provider returned no rows for the range.

    Deliberately distinct from a plain BrokerError: this is a *permanent*
    condition for the given request, not a transient outage, so callers must
    neither retry it nor count it against a circuit breaker (see
    core/reliability.py's `not_a_failure`), and the UI must surface it as
    DATA_INSUFFICIENT rather than analyzing against data it doesn't have.
    """


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
