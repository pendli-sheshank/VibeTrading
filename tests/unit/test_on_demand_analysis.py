from __future__ import annotations

import pytest

from tests.unit.test_market_snapshot import StubProvider, make_candles
from vibetrading.agents.on_demand import analyze_stock, combined_confidence
from vibetrading.config import Settings
from vibetrading.core.enums import AgentType, DataStatus, MarketDirection
from vibetrading.core.exceptions import BrokerError
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.marketdata.providers import SimulatedMarketDataProvider

STOCK = Stock(symbol="RELIANCE")


def settings() -> Settings:
    return Settings(_env_file=None)


async def test_analysis_over_real_data_produces_a_data_driven_confidence():
    result = await analyze_stock(market_data=SimulatedMarketDataProvider(seed=3), settings=settings(), stock=STOCK)

    assert result.analyzed is True
    assert result.technical is not None
    assert result.confidence is not None
    # Data-driven: a real number strictly inside the range, not a constant.
    assert 0.0 < result.confidence <= 1.0
    assert result.direction != MarketDirection.UNKNOWN
    assert result.technical.raw_data["rsi_14"] is not None


async def test_insufficient_data_yields_data_insufficient_not_a_zero_percent_result():
    """The bug this exists for: a provider with no usable data used to produce
    a 0%-confidence card that was indistinguishable from a real analysis
    finding no conviction."""
    result = await analyze_stock(market_data=StubProvider(candles=make_candles(5)), settings=settings(), stock=STOCK)

    assert result.status == DataStatus.DATA_INSUFFICIENT
    assert result.analyzed is False
    assert result.confidence is None  # NOT 0.0
    assert result.technical is None
    assert result.error is not None


async def test_provider_failure_surfaces_the_real_reason_instead_of_a_silent_zero():
    provider = StubProvider(candle_error=BrokerError("Dhan rejected historical_daily_data: DH-905 : Invalid security id"))

    result = await analyze_stock(market_data=provider, settings=settings(), stock=STOCK)

    assert result.analyzed is False
    assert result.confidence is None
    assert "DH-905" in result.error


async def test_a_failing_research_agent_does_not_hide_a_good_technical_read(monkeypatch):
    """One agent failing must not blank out the other -- and the failure is
    stated, not swallowed."""
    from vibetrading.agents.research.agent import ResearchAgent

    async def boom(self, stock, context):
        raise BrokerError("LLM provider unreachable")

    monkeypatch.setattr(ResearchAgent, "analyze", boom)

    result = await analyze_stock(market_data=SimulatedMarketDataProvider(seed=5), settings=settings(), stock=STOCK)

    assert result.technical is not None
    assert result.research is None
    assert result.analyzed is True
    assert result.confidence == pytest.approx(result.technical.confidence)
    assert any("LLM provider unreachable" in m for m in result.messages)


def test_combined_confidence_weights_both_agents_and_is_none_when_neither_reported():
    technical = AgentOutput(
        agent_type=AgentType.TECHNICAL,
        stock_symbol="X",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        confidence=0.8,
        summary="t",
    )
    research = technical.model_copy(update={"agent_type": AgentType.RESEARCH, "confidence": 0.4})

    assert combined_confidence(None, None) is None
    assert combined_confidence(technical, None) == pytest.approx(0.8)
    assert combined_confidence(technical, research) == pytest.approx(0.8 * 0.65 + 0.4 * 0.35)


async def test_analysis_result_serializes_for_the_json_api():
    result = await analyze_stock(market_data=SimulatedMarketDataProvider(seed=11), settings=settings(), stock=STOCK)
    payload = result.to_dict()

    assert payload["symbol"] == "RELIANCE"
    assert payload["analyzed"] is True
    assert payload["status"] == "simulated"
    assert payload["market_data"]["quote"]["status"] == "simulated"
    assert payload["technical"]["indicators"]["macd_line"] is not None
