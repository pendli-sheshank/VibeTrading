from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.agents.strategy.technical_agent import interpret_indicators
from vibetrading.agents.strategy.technical_indicators import (
    candles_to_dataframe,
    compute_all_indicators,
)
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import ActionType, AgentType
from vibetrading.core.models import AgentOutput, BacktestResult, Candle, Stock, TradeLogEntry

MIN_WARMUP_CANDLES = 20


@dataclass
class _OpenTrade:
    direction: str  # "long" | "short"
    entry_price: float
    entry_ts: datetime
    stop_loss: float
    quantity: int


class BacktestEngine:
    """Replays historical candles through StrategyAgent.synthesize() — the
    exact same code path the live pipeline uses — so a backtest validates
    the real strategy logic, not a reimplementation of it.

    Data limitation: only technical (price/volume) history is available
    for arbitrary past dates; Research Agent output (news/sentiment) has no
    historical archive to replay, so backtests run on technical signal
    only. This is a deliberate, documented scope narrowing, not a bug —
    treat backtest results as validating the technical half of the
    strategy, and expect live confidence (which also weighs research) to
    differ.
    """

    def __init__(self, broker: BrokerClient, strategy_agent: StrategyAgent, quantity: int = 1):
        self.broker = broker
        self.strategy_agent = strategy_agent
        self.quantity = quantity

    async def run(
        self, stock: Stock, start_date: datetime, end_date: datetime, warmup_days: int = 90
    ) -> BacktestResult:
        all_candles = await self.broker.get_historical_candles(
            stock, "1d", start_date - timedelta(days=warmup_days), end_date
        )
        all_candles = sorted(all_candles, key=lambda c: c.timestamp)

        trades: list[TradeLogEntry] = []
        open_trade: _OpenTrade | None = None
        running_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0

        in_range = [c for c in all_candles if start_date <= c.timestamp <= end_date]

        for candle in in_range:
            window = [c for c in all_candles if c.timestamp <= candle.timestamp]
            if len(window) < MIN_WARMUP_CANDLES:
                continue

            if open_trade is not None:
                stopped_out = (
                    (open_trade.direction == "long" and candle.low <= open_trade.stop_loss)
                    or (open_trade.direction == "short" and candle.high >= open_trade.stop_loss)
                )
                if stopped_out:
                    pnl = self._close_trade(stock, open_trade, open_trade.stop_loss, candle.timestamp, trades)
                    running_pnl += pnl
                    open_trade = None

            technical_output = self._technical_output(stock, window)
            signal = await self.strategy_agent.synthesize(stock, [technical_output])

            if open_trade is None:
                if signal.action == ActionType.BUY:
                    open_trade = _OpenTrade(
                        direction="long",
                        entry_price=candle.close,
                        entry_ts=candle.timestamp,
                        stop_loss=signal.suggested_stop_loss or candle.close * 0.97,
                        quantity=self.quantity,
                    )
                elif signal.action == ActionType.SELL:
                    open_trade = _OpenTrade(
                        direction="short",
                        entry_price=candle.close,
                        entry_ts=candle.timestamp,
                        stop_loss=signal.suggested_stop_loss or candle.close * 1.03,
                        quantity=self.quantity,
                    )
            else:
                opposite_signal = (open_trade.direction == "long" and signal.action == ActionType.SELL) or (
                    open_trade.direction == "short" and signal.action == ActionType.BUY
                )
                if opposite_signal:
                    pnl = self._close_trade(stock, open_trade, candle.close, candle.timestamp, trades)
                    running_pnl += pnl
                    open_trade = None

            peak_pnl = max(peak_pnl, running_pnl)
            max_drawdown = max(max_drawdown, peak_pnl - running_pnl)

        if open_trade is not None and in_range:
            last_candle = in_range[-1]
            pnl = self._close_trade(stock, open_trade, last_candle.close, last_candle.timestamp, trades)
            running_pnl += pnl
            max_drawdown = max(max_drawdown, max(peak_pnl, running_pnl) - running_pnl)

        wins = sum(1 for t in trades if (t.pnl or 0.0) > 0)
        win_rate = round(wins / len(trades), 4) if trades else 0.0

        return BacktestResult(
            stock_symbol=stock.symbol,
            start_date=start_date,
            end_date=end_date,
            total_trades=len(trades),
            win_rate=win_rate,
            total_pnl=round(running_pnl, 2),
            max_drawdown=round(max_drawdown, 2),
            trades=trades,
        )

    def _technical_output(self, stock: Stock, window: list[Candle]) -> AgentOutput:
        df = candles_to_dataframe(window)
        indicators = compute_all_indicators(df)
        summary, confidence = interpret_indicators(indicators)
        return AgentOutput(
            agent_type=AgentType.TECHNICAL,
            stock_symbol=stock.symbol,
            timestamp=window[-1].timestamp,
            confidence=confidence,
            summary=summary,
            raw_data=indicators,
        )

    def _close_trade(
        self,
        stock: Stock,
        open_trade: _OpenTrade,
        exit_price: float,
        exit_ts: datetime,
        trades: list[TradeLogEntry],
    ) -> float:
        if open_trade.direction == "long":
            pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
        else:
            pnl = (open_trade.entry_price - exit_price) * open_trade.quantity

        trades.append(
            TradeLogEntry(
                stock_symbol=stock.symbol,
                direction=open_trade.direction,
                entry_timestamp=open_trade.entry_ts,
                exit_timestamp=exit_ts,
                entry_price=open_trade.entry_price,
                exit_price=exit_price,
                quantity=open_trade.quantity,
                pnl=round(pnl, 2),
            )
        )
        return pnl
