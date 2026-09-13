from enum import StrEnum


class AgentType(StrEnum):
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


class DataStatus(StrEnum):
    """How much to trust one section of a MarketSnapshot.

    Every market-data field the dashboard shows carries one of these, so a
    reader can always tell a real live quote from a simulated one and from
    an absent one. Nothing is ever rendered as a number without a status
    beside it, and no analysis runs against DATA_INSUFFICIENT input.
    """

    LIVE = "live"  # fetched from the broker just now
    DELAYED = "delayed"  # real, but older than the freshness window
    SIMULATED = "simulated"  # MockBrokerClient's synthetic series — never real
    UNAVAILABLE = "unavailable"  # this provider cannot supply this data at all
    DATA_INSUFFICIENT = "data_insufficient"  # provider reachable, but returned too little to use
    ERROR = "error"  # the fetch itself failed; see the accompanying message


class MarketDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"
