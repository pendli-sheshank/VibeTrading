from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from vibetrading.agents.base import Agent
from vibetrading.agents.strategy.technical_indicators import (
    candles_to_dataframe,
    compute_all_indicators,
)
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Stock


class TechnicalAgent(Agent):
    """Computes technical indicators for a stock from historical candles.

    This is the "indicator step" of the Strategy Agent per the architecture:
    it produces a raw AgentOutput(agent_type=TECHNICAL); StrategyAgent (built
    in a later phase) combines this with Research Agent output into a Signal.
    """

    agent_type = AgentType.TECHNICAL

    def __init__(self, broker: BrokerClient, lookback_days: int = 120, run_interval_seconds: int = 300):
        self.broker = broker
        self.lookback_days = lookback_days
        self.run_interval_seconds = run_interval_seconds

    async def analyze(self, stock: Stock, context: dict[str, Any]) -> AgentOutput:
        now = datetime.now(UTC)
        candles = await self.broker.get_historical_candles(
            stock, "1d", now - timedelta(days=self.lookback_days), now
        )

        if len(candles) < 2:
            return AgentOutput(
                agent_type=self.agent_type,
                stock_symbol=stock.symbol,
                timestamp=now,
                confidence=0.0,
                summary="Insufficient historical data to compute indicators.",
                raw_data={},
            )

        df = candles_to_dataframe(candles)
        indicators = compute_all_indicators(df)
        summary, confidence = _interpret(indicators)

        return AgentOutput(
            agent_type=self.agent_type,
            stock_symbol=stock.symbol,
            timestamp=now,
            confidence=confidence,
            summary=summary,
            raw_data=indicators,
        )


def _interpret(indicators: dict) -> tuple[str, float]:
    """Turn raw indicator values into a human-readable summary and a rough
    confidence score (0-1) that momentum favors a directional move. This is
    intentionally simple, rule-of-thumb interpretation — the LLM-backed
    Strategy Agent (later phase) does the actual trade reasoning; this just
    gives it a clean, pre-digested technical read to work from.
    """
    rsi = indicators.get("rsi_14")
    macd_hist = indicators.get("macd_histogram")
    macd_hist_prev = indicators.get("macd_histogram_prev")
    close = indicators.get("close")
    sma_20 = indicators.get("sma_20")
    vol_trend = indicators.get("volume_trend")

    if rsi is None or close is None:
        return "Not enough history for a full technical read yet.", 0.2

    signals: list[str] = []
    bullish_points = 0
    bearish_points = 0

    if rsi >= 70:
        signals.append(f"RSI {rsi:.1f} indicates overbought conditions")
        bearish_points += 1
    elif rsi <= 30:
        signals.append(f"RSI {rsi:.1f} indicates oversold conditions")
        bullish_points += 1
    else:
        signals.append(f"RSI {rsi:.1f} is in neutral territory")

    if macd_hist is not None and macd_hist_prev is not None:
        if macd_hist > 0 and macd_hist > macd_hist_prev:
            signals.append("MACD histogram rising and positive (bullish momentum)")
            bullish_points += 1
        elif macd_hist < 0 and macd_hist < macd_hist_prev:
            signals.append("MACD histogram falling and negative (bearish momentum)")
            bearish_points += 1

    if sma_20 is not None:
        if close > sma_20:
            signals.append("price trading above its 20-day average")
            bullish_points += 1
        else:
            signals.append("price trading below its 20-day average")
            bearish_points += 1

    if vol_trend is not None and vol_trend > 1.3:
        signals.append(f"volume trend elevated ({vol_trend:.2f}x)")

    total_points = bullish_points + bearish_points
    if total_points == 0:
        confidence = 0.3
    else:
        confidence = 0.4 + 0.5 * (max(bullish_points, bearish_points) / max(total_points, 1))

    summary = "; ".join(signals)
    return summary, round(min(confidence, 1.0), 2)
