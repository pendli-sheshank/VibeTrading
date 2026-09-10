from __future__ import annotations

import litellm

from vibetrading.core.exceptions import LLMError
from vibetrading.core.reliability import with_retry_and_circuit_breaker
from vibetrading.llm.base import LLMAdapter, LLMResponse, Message


def _llm_circuit_name(self: LiteLLMAdapter, *args, **kwargs) -> str:
    """One breaker per provider (not per tenant/API key) -- a completion
    call has no side effects, so grouping by provider is enough to stop
    hammering an already-struggling one without needing per-tenant
    granularity the way Dhan's per-account breaker does."""
    return f"llm:{self.provider}"


class LiteLLMAdapter(LLMAdapter):
    """The one real LLMAdapter implementation, backed by LiteLLM's unified
    `acompletion()` across Anthropic/OpenAI/Gemini/OpenRouter. Agent code
    imports LLMAdapter, never litellm directly, so the underlying library
    can change without touching agent code.
    """

    def __init__(self, provider: str, model: str, api_key: str):
        self.provider = provider
        self.model = model
        self.api_key = api_key

    @with_retry_and_circuit_breaker(_llm_circuit_name, retry_on=(LLMError,))
    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        litellm_messages = [{"role": "system", "content": system}]
        litellm_messages += [{"role": m.role, "content": m.content} for m in messages]

        try:
            response = await litellm.acompletion(
                model=model or self.model,
                messages=litellm_messages,
                api_key=self.api_key,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise LLMError(f"LLM call to {self.provider} ({model or self.model}) failed: {exc}") from exc

        choice = response.choices[0]
        content = choice.message.content or ""
        raw = response.model_dump() if hasattr(response, "model_dump") else {}
        return LLMResponse(content=content, model=model or self.model, raw=raw)
