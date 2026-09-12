from __future__ import annotations

import pytest

from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.llm.providers.mock_provider import MockLLMAdapter


@pytest.fixture
def stock():
    return Stock(symbol="RELIANCE", name="Reliance Industries")


async def test_research_agent_requests_web_search(stock):
    llm = MockLLMAdapter()
    agent = ResearchAgent(llm=llm)

    await agent.analyze(stock, context={})

    assert len(llm.calls) == 1
    assert llm.calls[0]["enable_web_search"] is True
    assert "RELIANCE" in llm.calls[0]["messages"][0].content


async def test_research_agent_uses_llm_json_when_available(stock):
    llm = MockLLMAdapter(default_response='{"summary": "News and chatter both lean positive.", "confidence": 0.7}')
    agent = ResearchAgent(llm=llm)

    output = await agent.analyze(stock, context={})

    assert output.agent_type == AgentType.RESEARCH
    assert output.stock_symbol == "RELIANCE"
    assert output.summary == "News and chatter both lean positive."
    assert output.confidence == pytest.approx(0.7)


async def test_research_agent_clamps_out_of_range_confidence(stock):
    llm = MockLLMAdapter(default_response='{"summary": "Overconfident.", "confidence": 1.5}')
    agent = ResearchAgent(llm=llm)

    output = await agent.analyze(stock, context={})

    assert output.confidence == pytest.approx(1.0)


async def test_research_agent_falls_back_on_unparseable_llm_output(stock):
    llm = MockLLMAdapter(default_response="not valid json, just a plain-text answer")
    agent = ResearchAgent(llm=llm)

    output = await agent.analyze(stock, context={})

    assert output.summary == "not valid json, just a plain-text answer"
    assert output.confidence == pytest.approx(0.2)


async def test_research_agent_defaults_to_mock_llm_when_none_given(stock):
    agent = ResearchAgent()

    output = await agent.analyze(stock, context={})

    assert output.agent_type == AgentType.RESEARCH
    assert 0.0 <= output.confidence <= 1.0
