from __future__ import annotations

from vibetrading.llm.base import LLMAdapter, LLMResponse, Message

_SAFE_DEFAULT_SIGNAL_JSON = (
    '{"action": "hold", "confidence": 0.5, '
    '"reasoning": "Mock LLM: no provider configured, defaulting to HOLD.", '
    '"stop_loss_pct": null}'
)

# Research Agent's system prompt (see agents/research/agent.py) asks for a
# {"summary": ..., "confidence": ...} shape, not the {"action": ...} signal
# shape above -- a bare, no-args MockLLMAdapter (LLMRouter's automatic
# fallback with no API key configured) needs to answer with a schema that
# matches whichever agent is actually calling it, or the mismatched shape
# just shows up as raw JSON text in the Research card.
_SAFE_DEFAULT_RESEARCH_JSON = (
    '{"summary": "No LLM provider configured, so no web search was run for '
    'news/sentiment. Add an LLM API key under Settings to get real Research '
    'Agent reads.", "confidence": 0.2}'
)


class MockLLMAdapter(LLMAdapter):
    """Deterministic, no-network LLM adapter.

    Used automatically by LLMRouter when no provider API key is configured,
    and directly in tests. With no explicit `default_response`, it answers
    with a schema matching whichever agent's system prompt called it (a safe
    HOLD signal for the Strategy Agent, a "not configured" research summary
    for the Research Agent) -- running with no real LLM configured should
    never accidentally bias the Strategy Agent toward a trade, nor render as
    a raw JSON blob in the Research Agent's card. Tests can override
    `default_response` or queue specific `responses` to exercise both the
    happy-path JSON parsing and each call site's fallback-on-bad-output
    behavior.
    """

    def __init__(self, default_response: str | None = None, responses: list[str] | None = None):
        self._explicit_default = default_response
        self._queue: list[str] = list(responses) if responses else []
        self.calls: list[dict] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        enable_web_search: bool = False,
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "messages": messages, "model": model, "enable_web_search": enable_web_search}
        )
        if self._queue:
            content = self._queue.pop(0)
        elif self._explicit_default is not None:
            content = self._explicit_default
        elif '"summary"' in system:
            content = _SAFE_DEFAULT_RESEARCH_JSON
        else:
            content = _SAFE_DEFAULT_SIGNAL_JSON
        return LLMResponse(content=content, model=model or "mock")
