from __future__ import annotations

from datetime import UTC, datetime

from vibetrading.agents.base import SynthesisAgent
from vibetrading.core.enums import ActionType, AgentType
from vibetrading.core.models import AgentOutput, Signal, Stock
from vibetrading.llm.base import LLMAdapter, Message
from vibetrading.llm.parsing import parse_json_response
from vibetrading.llm.providers.mock_provider import MockLLMAdapter

_SYSTEM_PROMPT = (
    "You are the Strategy Agent of an algorithmic trading system for Indian "
    "equities. You receive a technical-indicator read and a combined "
    "news/social research view for one stock. Decide an action: buy, sell, "
    "or hold. Give a confidence score from 0 to 1 (be conservative — reserve "
    "high confidence for when technical and research signals agree). Give a "
    "1-3 sentence reasoning citing the specific indicators/research points "
    "that drove the decision. If recommending buy or sell, optionally give a "
    "stop_loss_pct (fraction of entry price, e.g. 0.03 for 3%) — omit or use "
    "null for hold. This decision will be checked by a separate deterministic "
    "Risk Agent before any real order is placed, so give your honest best "
    "read rather than being risk-conservative on the agent's behalf. "
    'Respond ONLY as JSON: {"action": "buy"|"sell"|"hold", "confidence": '
    'number, "reasoning": string, "stop_loss_pct": number|null}.'
)


class StrategyAgent(SynthesisAgent):
    """Combines the Technical Agent's indicator read with the Research Agent's
    combined news/sentiment view into one trade Signal.
    """

    def __init__(self, llm: LLMAdapter | None = None):
        self.llm = llm or MockLLMAdapter()

    async def synthesize(self, stock: Stock, outputs: list[AgentOutput]) -> Signal:
        technical = _latest(outputs, AgentType.TECHNICAL)
        research = _latest(outputs, AgentType.RESEARCH)

        user_prompt = _build_prompt(stock, technical, research)
        response = await self.llm.complete(system=_SYSTEM_PROMPT, messages=[Message(role="user", content=user_prompt)])
        action, confidence, reasoning, stop_loss_pct = _parse_signal(response.content)

        reference_price = technical.raw_data.get("close") if technical else None
        suggested_stop_loss = _compute_stop_loss(action, reference_price, stop_loss_pct)

        contributing_ids = [o.id for o in (technical, research) if o is not None and o.id is not None]

        return Signal(
            stock_symbol=stock.symbol,
            timestamp=datetime.now(UTC),
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            contributing_output_ids=contributing_ids,
            suggested_stop_loss=suggested_stop_loss,
            reference_price=reference_price,
        )


def _latest(outputs: list[AgentOutput], agent_type: AgentType) -> AgentOutput | None:
    matches = [o for o in outputs if o.agent_type == agent_type]
    if not matches:
        return None
    return max(matches, key=lambda o: o.timestamp)


def _build_prompt(stock: Stock, technical: AgentOutput | None, research: AgentOutput | None) -> str:
    lines = [f"Stock: {stock.symbol}"]
    if technical:
        lines.append(f"Technical read (confidence {technical.confidence:.2f}): {technical.summary}")
        indicators = {k: v for k, v in technical.raw_data.items() if v is not None}
        lines.append(f"Technical indicator values: {indicators}")
    else:
        lines.append("Technical read: unavailable")

    if research:
        lines.append(f"Research view (confidence {research.confidence:.2f}): {research.summary}")
    else:
        lines.append("Research view: unavailable")

    return "\n".join(lines)


def _parse_signal(content: str) -> tuple[ActionType, float, str, float | None]:
    data = parse_json_response(content)
    if data is None:
        return ActionType.HOLD, 0.3, f"Could not parse LLM response, defaulting to HOLD. Raw: {content[:200]}", None

    try:
        action = ActionType(str(data.get("action", "hold")).lower())
    except ValueError:
        return ActionType.HOLD, 0.3, f"Unrecognized action in LLM response, defaulting to HOLD. Raw: {content[:200]}", None

    confidence = max(0.0, min(1.0, _to_float(data.get("confidence"), default=0.3)))
    reasoning = str(data.get("reasoning") or "No reasoning provided.")

    stop_loss_pct_raw = data.get("stop_loss_pct")
    stop_loss_pct = _to_float(stop_loss_pct_raw, default=None) if stop_loss_pct_raw is not None else None

    return action, confidence, reasoning, stop_loss_pct


def _compute_stop_loss(action: ActionType, reference_price: float | None, stop_loss_pct: float | None) -> float | None:
    if reference_price is None or stop_loss_pct is None:
        return None
    if action == ActionType.BUY:
        return round(reference_price * (1 - abs(stop_loss_pct)), 2)
    if action == ActionType.SELL:
        return round(reference_price * (1 + abs(stop_loss_pct)), 2)
    return None


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
