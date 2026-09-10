from __future__ import annotations

from vibetrading.llm.base import LLMAdapter, LLMResponse, Message

_SAFE_DEFAULT_SIGNAL_JSON = (
    '{"action": "hold", "confidence": 0.5, '
    '"reasoning": "Mock LLM: no provider configured, defaulting to HOLD.", '
    '"stop_loss_pct": null}'
)


class MockLLMAdapter(LLMAdapter):
    """Deterministic, no-network LLM adapter.

    Used automatically by LLMRouter when no provider API key is configured,
    and directly in tests. Its default response is a safe HOLD signal —
    running with no real LLM configured should never accidentally bias the
    Strategy Agent toward a trade. Tests can override `default_response` or
    queue specific `responses` to exercise both the happy-path JSON parsing
    and each call site's fallback-on-bad-output behavior.
    """

    def __init__(self, default_response: str | None = None, responses: list[str] | None = None):
        self.default_response = default_response if default_response is not None else _SAFE_DEFAULT_SIGNAL_JSON
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
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": messages, "model": model})
        content = self._queue.pop(0) if self._queue else self.default_response
        return LLMResponse(content=content, model=model or "mock")
