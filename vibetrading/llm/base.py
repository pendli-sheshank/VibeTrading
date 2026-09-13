from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    raw: dict = {}


class LLMAdapter(ABC):
    """Every LLM provider (Anthropic, OpenAI, Gemini, OpenRouter, or a test
    mock) implements this one contract, so agent code never imports a
    provider SDK directly and stays swappable/testable.
    """

    @abstractmethod
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
        """enable_web_search asks the provider's own hosted web-search tool
        (not a locally-fetched news/social API) to ground the response in
        current information. Providers/models that don't support it just
        answer from training knowledge instead -- see LiteLLMAdapter."""
        ...
