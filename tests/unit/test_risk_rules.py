from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vibetrading.core.enums import ActionType, KillSwitchMode, SignalSource
from vibetrading.core.models import Signal, Stock
from vibetrading.persistence.orm_models import RiskStateORM
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.rules import (
    DailyLossCircuitBreakerRule,
    ExposureLimitRule,
    KillSwitchRule,
    MandatoryStopLossRule,
    MaxConcurrentPositionsRule,
    MaxPositionSizeRule,
    MinConfidenceRule,
    OrderValidationRule,
    RiskContext,
)

STOCK = Stock(symbol="TCS")


def make_config(**overrides) -> RiskConfig:
    defaults = dict(
        max_position_size_inr=50_000,
        max_pct_capital_per_stock=0.10,
        max_concurrent_positions=5,
        max_daily_loss_inr=10_000,
        mandatory_stop_loss_pct=0.03,
        min_signal_confidence=0.65,
        max_total_exposure_pct=0.50,
        kill_switch_mode=KillSwitchMode.HALT_NEW_ORDERS,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def make_risk_state(**overrides) -> RiskStateORM:
    defaults = dict(
        id=1,
        kill_switch_active=False,
        kill_switch_mode=KillSwitchMode.HALT_NEW_ORDERS.value,
        daily_realized_pnl=0.0,
        daily_pnl_date="2025-01-01",
        updated_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return RiskStateORM(**defaults)


def make_signal(**overrides) -> Signal:
    defaults = dict(
        stock_symbol="TCS",
        timestamp=datetime.now(UTC),
        source=SignalSource.STRATEGY_AGENT,
        action=ActionType.BUY,
        confidence=0.8,
        reasoning="test",
        reference_price=100.0,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_ctx(
    signal: Signal | None = None,
    config: RiskConfig | None = None,
    risk_state: RiskStateORM | None = None,
    **overrides,
) -> RiskContext:
    defaults = dict(
        signal=signal or make_signal(),
        stock=STOCK,
        config=config or make_config(),
        risk_state=risk_state or make_risk_state(),
        open_position_count=0,
        has_existing_position=False,
        available_funds=1_000_000.0,
        total_exposure_inr=0.0,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


# --- KillSwitchRule -----------------------------------------------------


def test_kill_switch_inactive_passes():
    ctx = make_ctx(risk_state=make_risk_state(kill_switch_active=False))
    outcome = KillSwitchRule().check(ctx)
    assert outcome.passed


def test_kill_switch_halt_new_orders_blocks_strategy_signal():
    ctx = make_ctx(risk_state=make_risk_state(kill_switch_active=True, kill_switch_mode="halt_new_orders"))
    outcome = KillSwitchRule().check(ctx)
    assert not outcome.passed


def test_kill_switch_halt_new_orders_allows_stop_loss_exit():
    ctx = make_ctx(
        signal=make_signal(source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL),
        risk_state=make_risk_state(kill_switch_active=True, kill_switch_mode="halt_new_orders"),
    )
    outcome = KillSwitchRule().check(ctx)
    assert outcome.passed


def test_kill_switch_halt_all_blocks_even_stop_loss_exit():
    ctx = make_ctx(
        signal=make_signal(source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL),
        risk_state=make_risk_state(kill_switch_active=True, kill_switch_mode="halt_all"),
    )
    outcome = KillSwitchRule().check(ctx)
    assert not outcome.passed


# --- DailyLossCircuitBreakerRule -----------------------------------------


def test_daily_loss_within_limit_passes():
    ctx = make_ctx(risk_state=make_risk_state(daily_realized_pnl=-5_000), config=make_config(max_daily_loss_inr=10_000))
    outcome = DailyLossCircuitBreakerRule().check(ctx)
    assert outcome.passed


def test_daily_loss_at_exact_limit_trips():
    ctx = make_ctx(risk_state=make_risk_state(daily_realized_pnl=-10_000), config=make_config(max_daily_loss_inr=10_000))
    outcome = DailyLossCircuitBreakerRule().check(ctx)
    assert not outcome.passed


def test_daily_loss_breached_still_allows_stop_loss_exit():
    ctx = make_ctx(
        signal=make_signal(source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL),
        risk_state=make_risk_state(daily_realized_pnl=-20_000),
        config=make_config(max_daily_loss_inr=10_000),
    )
    outcome = DailyLossCircuitBreakerRule().check(ctx)
    assert outcome.passed


# --- MinConfidenceRule ----------------------------------------------------


def test_confidence_above_minimum_passes():
    ctx = make_ctx(signal=make_signal(confidence=0.7), config=make_config(min_signal_confidence=0.65))
    assert MinConfidenceRule().check(ctx).passed


def test_confidence_below_minimum_fails():
    ctx = make_ctx(signal=make_signal(confidence=0.5), config=make_config(min_signal_confidence=0.65))
    assert not MinConfidenceRule().check(ctx).passed


def test_confidence_rule_exempts_hold():
    ctx = make_ctx(signal=make_signal(confidence=0.0, action=ActionType.HOLD))
    assert MinConfidenceRule().check(ctx).passed


# --- MaxPositionSizeRule ---------------------------------------------------


def test_position_size_capped_by_notional():
    ctx = make_ctx(
        signal=make_signal(reference_price=100.0, suggested_quantity=1000),
        config=make_config(max_position_size_inr=5_000, max_pct_capital_per_stock=1.0),
        available_funds=1_000_000.0,
    )
    outcome = MaxPositionSizeRule().check(ctx)
    assert outcome.passed
    assert ctx.quantity == 50  # 5000 / 100


def test_position_size_capped_by_available_funds():
    ctx = make_ctx(
        signal=make_signal(reference_price=100.0, suggested_quantity=1000),
        config=make_config(max_position_size_inr=1_000_000, max_pct_capital_per_stock=0.10),
        available_funds=10_000.0,
    )
    outcome = MaxPositionSizeRule().check(ctx)
    assert outcome.passed
    assert ctx.quantity == 10  # 10% of 10,000 / 100


def test_position_size_zero_when_no_reference_price():
    ctx = make_ctx(signal=make_signal(reference_price=None))
    outcome = MaxPositionSizeRule().check(ctx)
    assert not outcome.passed
    assert ctx.quantity == 0


def test_position_size_zero_funds_fails():
    ctx = make_ctx(signal=make_signal(reference_price=100.0), available_funds=0.0)
    outcome = MaxPositionSizeRule().check(ctx)
    assert not outcome.passed


def test_position_size_protective_exit_uses_exact_quantity_not_capped():
    ctx = make_ctx(
        signal=make_signal(
            source=SignalSource.SYSTEM_STOP_LOSS,
            action=ActionType.SELL,
            reference_price=100.0,
            suggested_quantity=500,  # far above the notional/pct caps below
        ),
        config=make_config(max_position_size_inr=1_000, max_pct_capital_per_stock=0.01),
        available_funds=1_000.0,
    )
    outcome = MaxPositionSizeRule().check(ctx)
    assert outcome.passed
    assert ctx.quantity == 500  # not capped down to 10 (1000/100) or 0 (1% of 1000 / 100)


def test_position_size_protective_exit_without_quantity_fails():
    ctx = make_ctx(
        signal=make_signal(source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL, suggested_quantity=None)
    )
    outcome = MaxPositionSizeRule().check(ctx)
    assert not outcome.passed
    assert ctx.quantity == 0


# --- MaxConcurrentPositionsRule -------------------------------------------


def test_concurrent_positions_under_limit_passes():
    ctx = make_ctx(open_position_count=2, config=make_config(max_concurrent_positions=5))
    assert MaxConcurrentPositionsRule().check(ctx).passed


def test_concurrent_positions_at_limit_fails():
    ctx = make_ctx(open_position_count=5, config=make_config(max_concurrent_positions=5))
    assert not MaxConcurrentPositionsRule().check(ctx).passed


def test_concurrent_positions_rule_ignores_existing_position():
    ctx = make_ctx(open_position_count=5, has_existing_position=True, config=make_config(max_concurrent_positions=5))
    assert MaxConcurrentPositionsRule().check(ctx).passed


# --- ExposureLimitRule -----------------------------------------------------


def test_exposure_within_limit_passes():
    ctx = make_ctx(
        signal=make_signal(reference_price=100.0),
        config=make_config(max_total_exposure_pct=0.5),
        available_funds=100_000.0,
        total_exposure_inr=0.0,
    )
    ctx.quantity = 100  # order_value = 10,000; total_capital = 100,000; limit = 50,000
    outcome = ExposureLimitRule().check(ctx)
    assert outcome.passed


def test_exposure_exceeding_limit_fails():
    ctx = make_ctx(
        signal=make_signal(reference_price=100.0),
        config=make_config(max_total_exposure_pct=0.1),
        available_funds=10_000.0,
        total_exposure_inr=0.0,
    )
    ctx.quantity = 500  # order_value = 50,000; total_capital = 10,000; limit = 1,000
    outcome = ExposureLimitRule().check(ctx)
    assert not outcome.passed


def test_exposure_limit_exempts_protective_exit_even_over_limit():
    ctx = make_ctx(
        signal=make_signal(source=SignalSource.SYSTEM_STOP_LOSS, action=ActionType.SELL, reference_price=100.0),
        config=make_config(max_total_exposure_pct=0.01),
        available_funds=100.0,
        total_exposure_inr=1_000_000.0,  # already way over any sane limit
    )
    ctx.quantity = 500  # would massively exceed the limit if this were a new entry
    outcome = ExposureLimitRule().check(ctx)
    assert outcome.passed


# --- MandatoryStopLossRule -------------------------------------------------


def test_stop_loss_uses_signal_suggestion_when_present():
    ctx = make_ctx(signal=make_signal(suggested_stop_loss=95.0))
    outcome = MandatoryStopLossRule().check(ctx)
    assert outcome.passed
    assert ctx.stop_loss_price == pytest.approx(95.0)


def test_stop_loss_applies_mandatory_default_for_buy():
    ctx = make_ctx(
        signal=make_signal(action=ActionType.BUY, reference_price=100.0, suggested_stop_loss=None),
        config=make_config(mandatory_stop_loss_pct=0.03),
    )
    outcome = MandatoryStopLossRule().check(ctx)
    assert outcome.passed
    assert ctx.stop_loss_price == pytest.approx(97.0)


def test_stop_loss_applies_mandatory_default_for_sell():
    ctx = make_ctx(
        signal=make_signal(action=ActionType.SELL, reference_price=100.0, suggested_stop_loss=None),
        config=make_config(mandatory_stop_loss_pct=0.03),
    )
    outcome = MandatoryStopLossRule().check(ctx)
    assert outcome.passed
    assert ctx.stop_loss_price == pytest.approx(103.0)


def test_stop_loss_fails_without_reference_price():
    ctx = make_ctx(signal=make_signal(reference_price=None, suggested_stop_loss=None))
    outcome = MandatoryStopLossRule().check(ctx)
    assert not outcome.passed


# --- OrderValidationRule ---------------------------------------------------


def test_order_validation_passes_with_positive_quantity():
    ctx = make_ctx()
    ctx.quantity = 10
    assert OrderValidationRule().check(ctx).passed


def test_order_validation_fails_with_zero_quantity():
    ctx = make_ctx()
    ctx.quantity = 0
    assert not OrderValidationRule().check(ctx).passed


def test_order_validation_skips_hold():
    ctx = make_ctx(signal=make_signal(action=ActionType.HOLD))
    assert OrderValidationRule().check(ctx).passed
