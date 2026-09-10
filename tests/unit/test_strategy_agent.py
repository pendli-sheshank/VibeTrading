from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.core.enums import ActionType, AgentType
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.llm.providers.mock_provider import MockLLMAdapter


@pytest.fixture
def stock():
    return Stock(symbol="TCS")


def technical_output(**raw_overrides) -> AgentOutput:
    raw_data = {"close": 3500.0, "rsi_14": 65.0, "sma_20": 3400.0}
    raw_data.update(raw_overrides)
    return AgentOutput(
        id=1,
        agent_type=AgentType.TECHNICAL,
        stock_symbol="TCS",
        timestamp=datetime.now(UTC),
        confidence=0.7,
        summary="Price above SMA-20, RSI neutral-bullish",
        raw_data=raw_data,
    )


def research_output() -> AgentOutput:
    return AgentOutput(
        id=2,
        agent_type=AgentType.RESEARCH,
        stock_symbol="TCS",
        timestamp=datetime.now(UTC),
        confidence=0.6,
        summary="News and sentiment both moderately positive",
        raw_data={},
    )


async def test_strategy_agent_produces_well_formed_buy_signal(stock):
    llm = MockLLMAdapter(
        default_response=(
            '{"action": "buy", "confidence": 0.82, '
            '"reasoning": "Price above SMA-20 with supportive research.", '
            '"stop_loss_pct": 0.03}'
        )
    )
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [technical_output(), research_output()])

    assert signal.action == ActionType.BUY
    assert signal.confidence == pytest.approx(0.82)
    assert signal.reasoning
    assert signal.stock_symbol == "TCS"
    assert signal.reference_price == pytest.approx(3500.0)
    assert signal.suggested_stop_loss == pytest.approx(3500.0 * 0.97)
    assert set(signal.contributing_output_ids) == {1, 2}


async def test_strategy_agent_sell_signal_stop_loss_is_above_reference(stock):
    llm = MockLLMAdapter(
        default_response='{"action": "sell", "confidence": 0.6, "reasoning": "Overbought.", "stop_loss_pct": 0.02}'
    )
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [technical_output(rsi_14=85.0), research_output()])

    assert signal.action == ActionType.SELL
    assert signal.suggested_stop_loss == pytest.approx(3500.0 * 1.02)


async def test_strategy_agent_hold_signal_has_no_stop_loss(stock):
    llm = MockLLMAdapter(
        default_response='{"action": "hold", "confidence": 0.4, "reasoning": "Mixed signals.", "stop_loss_pct": null}'
    )
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [technical_output(), research_output()])

    assert signal.action == ActionType.HOLD
    assert signal.suggested_stop_loss is None


async def test_strategy_agent_defaults_to_mock_llm_safe_hold(stock):
    # No LLM passed at all in production would route through LLMRouter, but a
    # bare MockLLMAdapter() (its default) must itself be the safe fallback.
    agent = StrategyAgent()
    signal = await agent.synthesize(stock, [technical_output(), research_output()])

    assert signal.action == ActionType.HOLD
    assert signal.confidence == pytest.approx(0.5)


async def test_strategy_agent_falls_back_to_hold_on_unparseable_output(stock):
    llm = MockLLMAdapter(default_response="the market feels bullish I guess")
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [technical_output(), research_output()])

    assert signal.action == ActionType.HOLD
    assert signal.confidence == pytest.approx(0.3)
    assert "Could not parse" in signal.reasoning


async def test_strategy_agent_falls_back_to_hold_on_invalid_action_value(stock):
    llm = MockLLMAdapter(default_response='{"action": "yolo", "confidence": 0.9, "reasoning": "x"}')
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [technical_output(), research_output()])

    assert signal.action == ActionType.HOLD
    assert "Unrecognized action" in signal.reasoning


async def test_strategy_agent_handles_missing_technical_output(stock):
    llm = MockLLMAdapter(default_response='{"action": "hold", "confidence": 0.3, "reasoning": "No technical data."}')
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [research_output()])

    assert signal.reference_price is None
    assert signal.suggested_stop_loss is None


async def test_strategy_agent_confidence_is_clamped(stock):
    llm = MockLLMAdapter(default_response='{"action": "buy", "confidence": 5.0, "reasoning": "x", "stop_loss_pct": 0.03}')
    agent = StrategyAgent(llm=llm)
    signal = await agent.synthesize(stock, [technical_output(), research_output()])

    assert signal.confidence == 1.0
