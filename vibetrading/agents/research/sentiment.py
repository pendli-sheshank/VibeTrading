from __future__ import annotations

from vibetrading.agents.research.chat_sources.base import RawMessage
from vibetrading.core.models import Stock
from vibetrading.llm.base import LLMAdapter, Message
from vibetrading.llm.parsing import parse_json_response

_SYSTEM_PROMPT = (
    "You are a social-sentiment analyst for Indian retail-investor chatter "
    "(X/Twitter, Reddit, StockTwits, forums). Given a sample of recent "
    "messages about a stock, summarize the net sentiment/buzz in 2-3 "
    "sentences, then give a confidence score from 0 to 1 for how strongly "
    "and consistently that sentiment leans one direction (0 = mixed/noisy, "
    '1 = strongly one-sided). Respond ONLY as JSON: '
    '{"summary": string, "confidence": number}.'
)


async def score_sentiment(llm: LLMAdapter, stock: Stock, messages: list[RawMessage]) -> tuple[str, float]:
    if not messages:
        return f"No social/forum chatter found for {stock.symbol}.", 0.2

    sample = "\n".join(f"- [{m.source}] {m.text}" for m in messages[:25])
    user_prompt = f"Stock: {stock.symbol}\nMessages:\n{sample}"

    response = await llm.complete(system=_SYSTEM_PROMPT, messages=[Message(role="user", content=user_prompt)])
    data = parse_json_response(response.content)

    if data and isinstance(data.get("summary"), str):
        confidence = _clamp(_to_float(data.get("confidence"), default=0.4))
        return data["summary"], confidence

    fallback_summary = (
        f"{len(messages)} messages collected for {stock.symbol}; "
        f"LLM summary unavailable/unparseable, raw: {response.content[:200]}"
    )
    return fallback_summary, 0.2


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
