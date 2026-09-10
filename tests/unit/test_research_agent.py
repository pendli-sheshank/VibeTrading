from __future__ import annotations

import pytest

from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.agents.research.chat_collector import ChatCollectorAgent
from vibetrading.agents.research.chat_sources.mock_source import MockChatSource
from vibetrading.agents.research.news_collector import NewsCollectorAgent
from vibetrading.agents.research.news_sources.mock_source import MockNewsSource
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.llm.providers.mock_provider import MockLLMAdapter


@pytest.fixture
def stock():
    return Stock(symbol="RELIANCE", name="Reliance Industries")


async def test_news_collector_against_mock_source(stock):
    agent = NewsCollectorAgent(source=MockNewsSource(), llm=MockLLMAdapter())
    output = await agent.analyze(stock, context={})

    assert output.agent_type == AgentType.NEWS
    assert output.stock_symbol == "RELIANCE"
    assert output.raw_data["item_count"] == 3


async def test_chat_collector_against_mock_source(stock):
    agent = ChatCollectorAgent(sources=[MockChatSource()], llm=MockLLMAdapter())
    output = await agent.analyze(stock, context={})

    assert output.agent_type == AgentType.CHAT
    assert output.raw_data["message_count"] == 5
    assert output.raw_data["sources"] == ["MockChatSource"]


async def test_news_summary_uses_llm_json_when_available(stock):
    llm = MockLLMAdapter(default_response='{"summary": "Positive earnings beat.", "confidence": 0.8}')
    agent = NewsCollectorAgent(source=MockNewsSource(), llm=llm)
    output = await agent.analyze(stock, context={})

    assert output.summary == "Positive earnings beat."
    assert output.confidence == pytest.approx(0.8)


async def test_news_summary_falls_back_on_unparseable_llm_output(stock):
    llm = MockLLMAdapter(default_response="not valid json")
    agent = NewsCollectorAgent(source=MockNewsSource(), llm=llm)
    output = await agent.analyze(stock, context={})

    assert "LLM summary unavailable" in output.summary
    assert output.confidence == pytest.approx(0.2)


async def test_research_agent_combines_news_and_chat(stock):
    llm = MockLLMAdapter(
        default_response='{"summary": "News and chatter both lean positive.", "confidence": 0.7}'
    )
    agent = ResearchAgent(
        news_agent=NewsCollectorAgent(source=MockNewsSource(), llm=MockLLMAdapter()),
        chat_agent=ChatCollectorAgent(sources=[MockChatSource()], llm=MockLLMAdapter()),
        llm=llm,
    )

    output = await agent.analyze(stock, context={})

    assert output.agent_type == AgentType.RESEARCH
    assert output.summary == "News and chatter both lean positive."
    assert output.confidence == pytest.approx(0.7)
    assert "news" in output.raw_data
    assert "chat" in output.raw_data


async def test_research_agent_falls_back_when_llm_output_unparseable(stock):
    llm = MockLLMAdapter(default_response="garbage")
    agent = ResearchAgent(
        news_agent=NewsCollectorAgent(source=MockNewsSource(), llm=MockLLMAdapter()),
        chat_agent=ChatCollectorAgent(sources=[MockChatSource()], llm=MockLLMAdapter()),
        llm=llm,
    )

    output = await agent.analyze(stock, context={})

    assert "News:" in output.summary and "Chat:" in output.summary
    assert 0.0 <= output.confidence <= 1.0


async def test_chat_collector_skips_failing_source_gracefully(stock):
    from vibetrading.agents.research.chat_sources.base import ChatSource

    class FailingSource(ChatSource):
        async def fetch(self, stock):
            raise RuntimeError("boom")

    agent = ChatCollectorAgent(sources=[MockChatSource(), FailingSource()], llm=MockLLMAdapter())
    output = await agent.analyze(stock, context={})

    # Only the mock source's messages should have made it through.
    assert output.raw_data["message_count"] == 5
