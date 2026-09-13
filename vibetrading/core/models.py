from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from vibetrading.core.enums import (
    ActionType,
    AgentType,
    DataStatus,
    ExecutionMode,
    MarketDirection,
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


class Quote(BaseModel):
    """A point-in-time price read for one instrument.

    Every field is optional except the symbol and status: a provider that
    only returns a last price must leave the rest None rather than have
    anything downstream invent an open/high/low from it.
    """

    symbol: str
    status: DataStatus
    last_price: float | None = None
    previous_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    timestamp: datetime | None = None
    source: str = "unknown"
    message: str | None = None

    @property
    def change(self) -> float | None:
        if self.last_price is None or self.previous_close is None:
            return None
        return round(self.last_price - self.previous_close, 2)

    @property
    def change_pct(self) -> float | None:
        if self.last_price is None or not self.previous_close:
            return None
        return round((self.last_price - self.previous_close) / self.previous_close * 100, 2)


class OptionStrike(BaseModel):
    strike: float
    call_oi: int | None = None
    call_oi_change: int | None = None
    call_volume: int | None = None
    call_iv: float | None = None
    call_ltp: float | None = None
    put_oi: int | None = None
    put_oi_change: int | None = None
    put_volume: int | None = None
    put_iv: float | None = None
    put_ltp: float | None = None


class OptionChainSnapshot(BaseModel):
    """Option-chain metrics around the money for one underlying.

    Only populated by brokers that actually expose an option chain. When a
    provider can't, the snapshot carries status UNAVAILABLE and empty
    strikes — the UI says so rather than showing a fabricated chain.
    """

    symbol: str
    status: DataStatus
    expiry: str | None = None
    underlying_price: float | None = None
    atm_strike: float | None = None
    strikes: list[OptionStrike] = Field(default_factory=list)
    total_call_oi: int | None = None
    total_put_oi: int | None = None
    put_call_ratio: float | None = None
    timestamp: datetime | None = None
    source: str = "unknown"
    message: str | None = None


class MarketSnapshot(BaseModel):
    """Everything known about an instrument right before an analysis runs.

    This is what the dashboard renders *before* the Analyze button does
    anything, so the inputs to an analysis are visible and auditable. It is
    also the gate: `is_sufficient_for_analysis` false means no analysis is
    attempted at all, rather than one produced from absent data.
    """

    symbol: str
    quote: Quote
    indicators: dict = Field(default_factory=dict)
    indicator_status: DataStatus = DataStatus.DATA_INSUFFICIENT
    # Why indicators couldn't be computed, when they couldn't. This is the
    # headline reason an analysis was blocked -- distinct from the other
    # messages, which may be about incidental sections (a missing option
    # chain never stopped anything).
    indicator_message: str | None = None
    candle_count: int = 0
    direction: MarketDirection = MarketDirection.UNKNOWN
    option_chain: OptionChainSnapshot | None = None
    generated_at: datetime
    source: str = "unknown"
    messages: list[str] = Field(default_factory=list)

    @property
    def is_sufficient_for_analysis(self) -> bool:
        return self.indicator_status in (DataStatus.LIVE, DataStatus.DELAYED, DataStatus.SIMULATED)


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
