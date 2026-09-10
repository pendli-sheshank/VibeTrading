from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from vibetrading.core.enums import ActionType, KillSwitchMode, SignalSource
from vibetrading.core.models import Signal, Stock
from vibetrading.persistence.orm_models import RiskStateORM
from vibetrading.risk.config import RiskConfig


@dataclass
class RiskContext:
    """Mutable working state threaded through the rule pipeline. Sizing and
    stop-loss rules fill in `quantity`/`stop_loss_price`; every rule (in a
    fixed order) can read whatever earlier rules have already computed.
    """

    signal: Signal
    stock: Stock
    config: RiskConfig
    risk_state: RiskStateORM
    open_position_count: int
    has_existing_position: bool
    available_funds: float
    total_exposure_inr: float
    quantity: int = 0
    stop_loss_price: float | None = None


@dataclass
class RuleOutcome:
    rule_name: str
    passed: bool
    reason: str


class RiskRule(ABC):
    name: str

    @abstractmethod
    def check(self, ctx: RiskContext) -> RuleOutcome:
        ...


def _is_protective_exit(signal: Signal) -> bool:
    return signal.source == SignalSource.SYSTEM_STOP_LOSS


class KillSwitchRule(RiskRule):
    name = "kill_switch"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if not ctx.risk_state.kill_switch_active:
            return RuleOutcome(self.name, True, "Kill switch inactive.")

        mode = ctx.risk_state.kill_switch_mode
        if mode == KillSwitchMode.HALT_ALL.value:
            return RuleOutcome(self.name, False, "Kill switch active in HALT_ALL mode; all orders blocked.")

        if mode == KillSwitchMode.HALT_NEW_ORDERS.value and not _is_protective_exit(ctx.signal):
            return RuleOutcome(
                self.name, False, "Kill switch active in HALT_NEW_ORDERS mode; new entries blocked."
            )

        return RuleOutcome(self.name, True, "Kill switch active but this is a protective exit; allowed.")


class DailyLossCircuitBreakerRule(RiskRule):
    name = "daily_loss_circuit_breaker"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        limit = abs(ctx.config.max_daily_loss_inr)
        tripped = ctx.risk_state.daily_realized_pnl <= -limit

        if not tripped:
            return RuleOutcome(
                self.name, True, f"Daily realized P&L {ctx.risk_state.daily_realized_pnl:.2f} within -{limit:.2f} limit."
            )

        if _is_protective_exit(ctx.signal):
            return RuleOutcome(self.name, True, "Daily loss limit breached but this is a protective exit; allowed.")

        return RuleOutcome(
            self.name,
            False,
            f"Daily loss limit breached (realized P&L {ctx.risk_state.daily_realized_pnl:.2f} <= -{limit:.2f}); "
            "new entries halted for today.",
        )


class MinConfidenceRule(RiskRule):
    name = "min_confidence"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.signal.action == ActionType.HOLD:
            return RuleOutcome(self.name, True, "HOLD action; confidence threshold not applicable.")
        if _is_protective_exit(ctx.signal):
            return RuleOutcome(self.name, True, "Protective exit; confidence threshold not applicable.")

        if ctx.signal.confidence >= ctx.config.min_signal_confidence:
            return RuleOutcome(
                self.name, True, f"Confidence {ctx.signal.confidence:.2f} meets minimum {ctx.config.min_signal_confidence:.2f}."
            )
        return RuleOutcome(
            self.name,
            False,
            f"Confidence {ctx.signal.confidence:.2f} below minimum {ctx.config.min_signal_confidence:.2f}.",
        )


class MaxPositionSizeRule(RiskRule):
    """Sizing rule: computes/caps ctx.quantity in place. This always "passes"
    unless the resulting order size is zero (e.g. no funds or no reference
    price) — it's corrective sizing, not a hard gate on its own.
    """

    name = "max_position_size"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.signal.action == ActionType.HOLD:
            ctx.quantity = 0
            return RuleOutcome(self.name, True, "HOLD action; no sizing needed.")

        if _is_protective_exit(ctx.signal):
            # A stop-loss exit must close the exact position size, never be
            # re-capped by entry sizing limits — capping it here could leave
            # a stub position open with no protection.
            ctx.quantity = ctx.signal.suggested_quantity or 0
            if ctx.quantity <= 0:
                return RuleOutcome(self.name, False, "Protective exit signal is missing a quantity to close.")
            return RuleOutcome(self.name, True, f"Protective exit: closing exact position size {ctx.quantity}.")

        price = ctx.signal.reference_price
        if price is None or price <= 0:
            ctx.quantity = 0
            return RuleOutcome(self.name, False, "No reference price available; cannot size order.")

        by_notional_cap = int(ctx.config.max_position_size_inr // price)
        by_pct_capital = int((ctx.config.max_pct_capital_per_stock * ctx.available_funds) // price)
        requested = ctx.signal.suggested_quantity or by_notional_cap

        quantity = max(0, min(requested, by_notional_cap, by_pct_capital))
        ctx.quantity = quantity

        if quantity <= 0:
            return RuleOutcome(
                self.name,
                False,
                f"Computed order size is zero (notional cap {by_notional_cap}, capital cap {by_pct_capital}, "
                f"available funds {ctx.available_funds:.2f}).",
            )
        return RuleOutcome(
            self.name, True, f"Sized to {quantity} shares (notional cap {by_notional_cap}, capital cap {by_pct_capital})."
        )


class MaxConcurrentPositionsRule(RiskRule):
    name = "max_concurrent_positions"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.signal.action == ActionType.HOLD or ctx.has_existing_position:
            return RuleOutcome(self.name, True, "Not opening a new position slot.")

        if ctx.open_position_count < ctx.config.max_concurrent_positions:
            return RuleOutcome(
                self.name,
                True,
                f"Open positions {ctx.open_position_count} below max {ctx.config.max_concurrent_positions}.",
            )
        return RuleOutcome(
            self.name, False, f"Max concurrent positions ({ctx.config.max_concurrent_positions}) reached."
        )


class ExposureLimitRule(RiskRule):
    name = "max_total_exposure"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.signal.action == ActionType.HOLD or ctx.quantity <= 0 or _is_protective_exit(ctx.signal):
            return RuleOutcome(self.name, True, "No new exposure being added (or this is a protective exit).")

        order_value = ctx.quantity * (ctx.signal.reference_price or 0.0)
        total_capital = ctx.available_funds + ctx.total_exposure_inr
        max_exposure = ctx.config.max_total_exposure_pct * total_capital if total_capital > 0 else 0.0
        projected = ctx.total_exposure_inr + order_value

        if projected <= max_exposure:
            return RuleOutcome(self.name, True, f"Projected exposure {projected:.2f} within limit {max_exposure:.2f}.")
        return RuleOutcome(self.name, False, f"Projected exposure {projected:.2f} exceeds limit {max_exposure:.2f}.")


class MandatoryStopLossRule(RiskRule):
    """Corrective, not blocking: fills in a default stop-loss when the signal
    didn't suggest one. Only fails when it's genuinely impossible to compute
    one (no reference price).
    """

    name = "mandatory_stop_loss"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.signal.action == ActionType.HOLD:
            return RuleOutcome(self.name, True, "HOLD action; no stop-loss required.")

        if ctx.signal.suggested_stop_loss is not None:
            ctx.stop_loss_price = ctx.signal.suggested_stop_loss
            return RuleOutcome(self.name, True, "Using Strategy Agent's suggested stop-loss.")

        if ctx.signal.reference_price is None:
            return RuleOutcome(self.name, False, "No reference price; cannot compute mandatory stop-loss.")

        pct = ctx.config.mandatory_stop_loss_pct
        if ctx.signal.action == ActionType.BUY:
            ctx.stop_loss_price = round(ctx.signal.reference_price * (1 - pct), 2)
        else:
            ctx.stop_loss_price = round(ctx.signal.reference_price * (1 + pct), 2)

        return RuleOutcome(self.name, True, f"No stop-loss suggested; applied mandatory default of {pct:.1%}.")


class OrderValidationRule(RiskRule):
    name = "order_validation"

    def check(self, ctx: RiskContext) -> RuleOutcome:
        if ctx.signal.action == ActionType.HOLD:
            return RuleOutcome(self.name, True, "HOLD action; nothing to validate.")
        if not ctx.stock.symbol:
            return RuleOutcome(self.name, False, "Missing stock symbol.")
        if ctx.quantity <= 0:
            return RuleOutcome(self.name, False, "Order quantity is not positive.")
        if ctx.signal.action not in (ActionType.BUY, ActionType.SELL):
            # EXIT is reserved for a future direction-aware exit signal; today
            # protective exits are emitted as a direct BUY/SELL with
            # source=SYSTEM_STOP_LOSS, so only those two reach the broker.
            return RuleOutcome(self.name, False, f"Unsupported action for order placement: {ctx.signal.action}.")
        return RuleOutcome(self.name, True, "Order passes basic validation.")


DEFAULT_RULES: list[RiskRule] = [
    KillSwitchRule(),
    DailyLossCircuitBreakerRule(),
    MinConfidenceRule(),
    MaxPositionSizeRule(),
    MaxConcurrentPositionsRule(),
    ExposureLimitRule(),
    MandatoryStopLossRule(),
    OrderValidationRule(),
]
