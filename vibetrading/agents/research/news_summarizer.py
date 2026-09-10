from __future__ import annotations

from vibetrading.agents.research.news_sources.base import NewsItem
from vibetrading.core.models import Stock
from vibetrading.llm.base import LLMAdapter, Message
from vibetrading.llm.parsing import parse_json_response

_SYSTEM_PROMPT = (
    "You are a financial news analyst covering Indian equities. Given recent "
    "headlines for a stock, summarize the net sentiment/impact in 2-3 "
    "sentences, then give a confidence score from 0 to 1 for how directional "
    "(as opposed to neutral/noise) the news is. "
    'Respond ONLY as JSON: {"summary": string, "confidence": number}.'
)


async def summarize_news(llm: LLMAdapter, stock: Stock, items: list[NewsItem]) -> tuple[str, float]:
    if not items:
        return f"No recent news found for {stock.symbol}.", 0.2

    headlines = "\n".join(f"- {item.title} ({item.source})" for item in items[:10])
    user_prompt = f"Stock: {stock.symbol}\nHeadlines:\n{headlines}"

    response = await llm.complete(system=_SYSTEM_PROMPT, messages=[Message(role="user", content=user_prompt)])
    data = parse_json_response(response.content)

    if data and isinstance(data.get("summary"), str):
        confidence = _clamp(_to_float(data.get("confidence"), default=0.4))
        return data["summary"], confidence

    fallback_summary = (
        f"{len(items)} recent headlines collected for {stock.symbol}; "
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
