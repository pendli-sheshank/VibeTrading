from __future__ import annotations

from pydantic import BaseModel

from vibetrading.config import Settings
from vibetrading.core.enums import KillSwitchMode


class RiskConfig(BaseModel):
    """Risk Agent limits, decoupled from the global Settings object so rules
    and the engine can be tested with an explicit config rather than reaching
    into env-derived global state."""

    max_position_size_inr: float
    max_pct_capital_per_stock: float
    max_concurrent_positions: int
    max_daily_loss_inr: float
    mandatory_stop_loss_pct: float
    min_signal_confidence: float
    max_total_exposure_pct: float
    kill_switch_mode: KillSwitchMode

    @classmethod
    def from_settings(cls, settings: Settings) -> RiskConfig:
        return cls(
            max_position_size_inr=settings.risk_max_position_size_inr,
            max_pct_capital_per_stock=settings.risk_max_pct_capital_per_stock,
            max_concurrent_positions=settings.risk_max_concurrent_positions,
            max_daily_loss_inr=settings.risk_max_daily_loss_inr,
            mandatory_stop_loss_pct=settings.risk_mandatory_stop_loss_pct,
            min_signal_confidence=settings.risk_min_signal_confidence,
            max_total_exposure_pct=settings.risk_max_total_exposure_pct,
            kill_switch_mode=settings.vibetrading_kill_switch_mode,
        )
