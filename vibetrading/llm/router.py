from __future__ import annotations

import logging

from vibetrading.config import Settings, get_settings
from vibetrading.core.enums import AgentType
from vibetrading.llm.base import LLMAdapter
from vibetrading.llm.litellm_adapter import LiteLLMAdapter
from vibetrading.llm.providers.mock_provider import MockLLMAdapter

logger = logging.getLogger(__name__)

# Best-effort defaults; model catalogs change frequently, so override per
# deployment via LLM_MODEL_RESEARCH / LLM_MODEL_STRATEGY / LLM_MODEL_BACKTEST
# (or by passing `model=` straight to LLMAdapter.complete) rather than
# trusting these long-term.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
    "gemini": "gemini-1.5-pro",
    "openrouter": "openrouter/auto",
}

_MODEL_OVERRIDE_ATTR = {
    AgentType.RESEARCH: "llm_model_research",
    AgentType.STRATEGY: "llm_model_strategy",
    AgentType.BACKTEST: "llm_model_backtest",
}


class LLMRouter:
    """Resolves which LLMAdapter + model an agent type should use.

    Falls back to MockLLMAdapter (loudly logged) whenever the configured
    provider has no API key set, so the platform runs end-to-end with zero
    LLM keys configured.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def get_adapter(self, agent_type: AgentType) -> LLMAdapter:
        provider = self.settings.llm_default_provider.lower()
        api_key = self.settings.llm_key_for(provider)

        if not api_key:
            logger.warning(
                "No API key configured for LLM provider '%s'; %s agent falling back to MockLLMAdapter.",
                provider,
                agent_type.value,
            )
            return MockLLMAdapter()

        model = self._model_for(agent_type, provider)
        return LiteLLMAdapter(provider=provider, model=model, api_key=api_key)

    def _model_for(self, agent_type: AgentType, provider: str) -> str:
        override_attr = _MODEL_OVERRIDE_ATTR.get(agent_type)
        override = getattr(self.settings, override_attr, "") if override_attr else ""
        if override:
            return override
        return DEFAULT_MODELS.get(provider, DEFAULT_MODELS["anthropic"])
