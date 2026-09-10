from enum import StrEnum


class AgentType(StrEnum):
    NEWS = "news"
    CHAT = "chat"
    RESEARCH = "research"
    TECHNICAL = "technical"
    STRATEGY = "strategy"
    BACKTEST = "backtest"


class ActionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    EXIT = "exit"


class ExecutionMode(StrEnum):
    LIVE = "live"
    PAPER = "paper"


class OrderStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class KillSwitchMode(StrEnum):
    HALT_NEW_ORDERS = "halt_new_orders"
    HALT_ALL = "halt_all"


class SignalSource(StrEnum):
    STRATEGY_AGENT = "strategy_agent"
    SYSTEM_STOP_LOSS = "system_stop_loss"
