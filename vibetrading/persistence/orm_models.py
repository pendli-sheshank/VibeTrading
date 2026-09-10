from __future__ import annotations

from datetime import datetime

from fastapi_users.db import SQLAlchemyBaseUserTable
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserORM(SQLAlchemyBaseUserTable[int], Base):
    """One row per account. `id` doubles as the tenant_id used to scope every
    other table (see persistence/repositories.py) -- one user is one tenant,
    by design (see the multi-tenant plan's tenant-model decision)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class StockORM(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    exchange: Mapped[str] = mapped_column(String(16), default="NSE")
    dhan_security_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentRunORM(Base):
    """Persisted AgentOutput — one row per Research/Technical agent run."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_type: Mapped[str] = mapped_column(String(32), index=True)
    stock_symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(String)
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)


class SignalORM(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(String)
    contributing_output_ids: Mapped[list] = mapped_column(JSON, default=list)
    suggested_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    suggested_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Realized outcome, filled in later by the performance tracker.
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OrderORM(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    stock_symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(8))
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)


class PositionORM(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLogORM(Base):
    """Every risk decision + order outcome, written PENDING before the broker call."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    contributing_agent_output_ids: Mapped[list] = mapped_column(JSON, default=list)
    risk_checks_passed: Mapped[dict] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class RiskEventORM(Base):
    """Standalone log of every risk-rule rejection/trip, for the Risk dashboard page."""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_name: Mapped[str] = mapped_column(String(64), index=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class RiskStateORM(Base):
    """Single row (id=1) holding the live Risk Agent state: kill switch and
    running daily realized P&L (reset when daily_pnl_date rolls over) — the
    source of truth beyond the env-configured default once the app is running.
    """

    __tablename__ = "risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch_mode: Mapped[str] = mapped_column(String(32), default="halt_new_orders")
    daily_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_date: Mapped[str] = mapped_column(String(10))  # ISO date, e.g. "2025-01-31"
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)


class BacktestRunORM(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_symbol: Mapped[str] = mapped_column(String(32), index=True)
    start_date: Mapped[datetime] = mapped_column(DateTime)
    end_date: Mapped[datetime] = mapped_column(DateTime)
    total_trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    max_drawdown: Mapped[float] = mapped_column(Float)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class SettingORM(Base):
    """One row per UI-editable application setting (see vibetrading/settings/).
    A key-value table rather than one column per field, so adding a future
    setting never needs a migration. `value` is JSON-encoded for plain
    values, or Fernet ciphertext (see settings/crypto.py) when is_secret.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
