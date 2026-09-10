from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from vibetrading.core.enums import (
    ActionType,
    AgentType,
    ExecutionMode,
    OrderSide,
    OrderStatus,
    SignalSource,
)


class Stock(BaseModel):
    symbol: str
    exchange: str = "NSE"
    dhan_security_id: str | None = None
    name: str | None = None
    sector: str | None = None

    @field_validator("symbol")
    @classmethod
    def _uppercase_symbol(cls, v: str) -> str:
        return v.strip().upper()


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class AgentOutput(BaseModel):
    """One agent's read on one stock at one point in time."""

    id: int | None = None  # set once persisted; lets downstream agents cite their sources
    agent_type: AgentType
    stock_symbol: str
    timestamp: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    raw_data: dict = Field(default_factory=dict)


class Signal(BaseModel):
    """The Strategy Agent's (or a system rule's) trade suggestion for a stock."""

    stock_symbol: str
    timestamp: datetime
    source: SignalSource = SignalSource.STRATEGY_AGENT
    action: ActionType
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    contributing_output_ids: list[int] = Field(default_factory=list)
    suggested_quantity: int | None = None
    suggested_stop_loss: float | None = None
    # Price the signal was generated against (e.g. latest technical close) —
    # the reference point performance_tracker.py reconciles later outcomes against.
    reference_price: float | None = None


class RiskCheckResult(BaseModel):
    """Full result of running a Signal through every Risk Agent rule.

    `rule_results` and `reasons` always cover every rule that ran, not just
    the first failure, so a rejected signal's reasoning is fully inspectable.
    """

    approved: bool
    rule_results: dict[str, bool] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    adjusted_quantity: int | None = None
    adjusted_stop_loss: float | None = None


class OrderRequest(BaseModel):
    stock_symbol: str
    side: OrderSide
    quantity: int
    order_type: str = "MARKET"
    limit_price: float | None = None
    stop_loss_price: float | None = None
    mode: ExecutionMode


class OrderResult(BaseModel):
    order_id: str
    broker_order_id: str | None = None
    status: OrderStatus
    filled_quantity: int = 0
    filled_price: float | None = None
    # Realized P&L this specific fill booked (0.0 for an opening/increasing
    # trade; nonzero when it reduced/closed an existing position). Used by
    # the Risk Agent's daily-loss circuit breaker. Best-effort per broker —
    # a broker that can't compute this cheaply may always report 0.0.
    realized_pnl: float = 0.0
    raw_response: dict = Field(default_factory=dict)


class Position(BaseModel):
    stock_symbol: str
    quantity: int
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    stop_loss_price: float | None = None
    opened_at: datetime


class FundsSnapshot(BaseModel):
    available_balance: float
    used_margin: float = 0.0


class AuditLogEntry(BaseModel):
    id: int | None = None
    order_id: str | None = None
    signal_id: int | None = None
    contributing_agent_output_ids: list[int] = Field(default_factory=list)
    risk_checks_passed: dict[str, bool] = Field(default_factory=dict)
    mode: ExecutionMode
    status: str
    timestamp: datetime


class TradeLogEntry(BaseModel):
    """One simulated/backtested trade for a BacktestResult's trade log."""

    stock_symbol: str
    direction: str = "long"  # "long" | "short"
    entry_timestamp: datetime
    exit_timestamp: datetime | None = None
    entry_price: float
    exit_price: float | None = None
    quantity: int
    pnl: float | None = None


class BacktestResult(BaseModel):
    stock_symbol: str
    start_date: datetime
    end_date: datetime
    total_trades: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    trades: list[TradeLogEntry] = Field(default_factory=list)
