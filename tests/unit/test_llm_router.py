from __future__ import annotations

from vibetrading.config import Settings
from vibetrading.core.enums import AgentType
from vibetrading.llm.litellm_adapter import LiteLLMAdapter
from vibetrading.llm.providers.mock_provider import MockLLMAdapter
from vibetrading.llm.router import LLMRouter


def test_router_falls_back_to_mock_when_no_key_configured():
    settings = Settings(_env_file=None, llm_default_provider="anthropic", anthropic_api_key="")
    router = LLMRouter(settings=settings)
    adapter = router.get_adapter(AgentType.STRATEGY)
    assert isinstance(adapter, MockLLMAdapter)


def test_router_uses_litellm_when_key_configured():
    settings = Settings(_env_file=None, llm_default_provider="anthropic", anthropic_api_key="sk-test")
    router = LLMRouter(settings=settings)
    adapter = router.get_adapter(AgentType.STRATEGY)
    assert isinstance(adapter, LiteLLMAdapter)
    assert adapter.api_key == "sk-test"
    assert adapter.model == "claude-sonnet-5"


def test_router_respects_per_agent_model_override():
    settings = Settings(
        _env_file=None,
        llm_default_provider="anthropic",
        anthropic_api_key="sk-test",
        llm_model_strategy="claude-custom-model",
    )
    router = LLMRouter(settings=settings)
    strategy_adapter = router.get_adapter(AgentType.STRATEGY)
    research_adapter = router.get_adapter(AgentType.RESEARCH)

    assert strategy_adapter.model == "claude-custom-model"
    assert research_adapter.model == "claude-sonnet-5"
