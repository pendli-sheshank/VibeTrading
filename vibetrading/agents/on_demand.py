from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.agents.strategy.technical_agent import TechnicalAgent
from vibetrading.broker.base import BrokerClient
from vibetrading.config import Settings
from vibetrading.core.enums import AgentType, DataStatus, MarketDirection
from vibetrading.core.exceptions import BrokerError, LLMError, MarketDataUnavailableError
from vibetrading.core.models import AgentOutput, MarketSnapshot, Stock
from vibetrading.core.reliability import CircuitBreakerOpenError
from vibetrading.llm.router import LLMRouter
from vibetrading.marketdata import build_market_snapshot
from vibetrading.persistence.repositories import save_agent_output

logger = logging.getLogger(__name__)

# How much each agent's confidence counts toward the combined figure. The
# technical read is weighted higher because it is computed from actual price
# history, whereas research is an LLM's reading of prose it found. These are
# weights on real per-agent numbers -- the combined value is never set to a
# constant, and is absent entirely when neither agent produced one.
TECHNICAL_WEIGHT = 0.65
RESEARCH_WEIGHT = 0.35


@dataclass
class AnalysisResult:
    """Outcome of one on-demand analysis.

    `analyzed` is the honest summary: False means nothing was computed (and
    `status` says why) rather than a zero-confidence result that reads like
    a real one.
    """

    symbol: str
    status: DataStatus
    snapshot: MarketSnapshot
    technical: AgentOutput | None = None
    research: AgentOutput | None = None
    direction: MarketDirection = MarketDirection.UNKNOWN
    confidence: float | None = None
    messages: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def analyzed(self) -> bool:
        return self.technical is not None or self.research is not None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "status": self.status.value,
            "analyzed": self.analyzed,
            "confidence": self.confidence,
            "direction": self.direction.value,
            "technical": _agent_dict(self.technical),
            "research": _agent_dict(self.research),
            "market_data": self.snapshot.model_dump(mode="json"),
            "messages": self.messages,
            "error": self.error,
        }


def _agent_dict(output: AgentOutput | None) -> dict | None:
    if output is None:
        return None
    return {
        "confidence": output.confidence,
        "summary": output.summary,
        "timestamp": output.timestamp,
        "indicators": output.raw_data,
    }


async def analyze_stock(
    broker: BrokerClient,
    settings: Settings,
    stock: Stock,
    session: AsyncSession | None = None,
    tenant_id: int | None = None,
) -> AnalysisResult:
    """Run the Research and Technical agents for one stock, right now.

    Order matters: the market snapshot is collected and checked FIRST. If
    the data needed for a technical read isn't there, no analysis runs at
    all and the result carries DATA_INSUFFICIENT with the provider's own
    explanation -- rather than the previous behavior, where a broker error
    was swallowed into a 0%-confidence card with no stated reason.

    Persists whatever it did produce when a session is supplied, exactly as
    a scheduled orchestrator cycle would.
    """
    snapshot = await build_market_snapshot(broker, stock)

    if not snapshot.is_sufficient_for_analysis:
        return AnalysisResult(
            symbol=stock.symbol,
            status=snapshot.indicator_status,
            snapshot=snapshot,
            direction=snapshot.direction,
            messages=snapshot.messages,
            # The reason indicators couldn't be computed -- not whichever
            # message happens to be first, which could be about a section
            # (like the option chain) that never blocked anything.
            error=snapshot.indicator_message or _first_message(snapshot.messages),
        )

    llm = LLMRouter(settings).get_adapter(AgentType.RESEARCH)
    technical_task = TechnicalAgent(broker=broker).analyze(stock, context={})
    research_task = ResearchAgent(llm=llm).analyze(stock, context={})
    technical_raw, research_raw = await asyncio.gather(technical_task, research_task, return_exceptions=True)

    messages = list(snapshot.messages)
    technical = _unwrap_agent(AgentType.TECHNICAL, technical_raw, stock, messages)
    research = _unwrap_agent(AgentType.RESEARCH, research_raw, stock, messages)

    if technical is None and research is None:
        return AnalysisResult(
            symbol=stock.symbol,
            status=DataStatus.ERROR,
            snapshot=snapshot,
            direction=snapshot.direction,
            messages=messages,
            error=_first_message(messages) or "Both agents failed; see server logs.",
        )

    if session is not None and tenant_id is not None:
        for output in (technical, research):
            if output is not None:
                await save_agent_output(session, tenant_id, output)

    return AnalysisResult(
        symbol=stock.symbol,
        status=snapshot.indicator_status,
        snapshot=snapshot,
        technical=technical,
        research=research,
        direction=snapshot.direction,
        confidence=combined_confidence(technical, research),
        messages=messages,
    )


def combined_confidence(technical: AgentOutput | None, research: AgentOutput | None) -> float | None:
    """Weighted mean of whichever agents actually reported.

    Returns None (not 0.0) when neither did: "no reading" and "a reading of
    zero" are different answers and must not render the same.
    """
    parts = [
        (output.confidence, weight)
        for output, weight in ((technical, TECHNICAL_WEIGHT), (research, RESEARCH_WEIGHT))
        if output is not None
    ]
    if not parts:
        return None
    total_weight = sum(weight for _, weight in parts)
    return round(sum(value * weight for value, weight in parts) / total_weight, 4)


def _unwrap_agent(
    agent_type: AgentType, outcome: AgentOutput | BaseException, stock: Stock, messages: list[str]
) -> AgentOutput | None:
    """Turn one gathered agent result into an output, or record why not.

    A failing agent yields None plus a human-readable reason, so the other
    agent's result still reaches the screen and the failure is explained
    instead of being flattened into a zero.
    """
    if isinstance(outcome, AgentOutput):
        return outcome

    if isinstance(outcome, MarketDataUnavailableError | BrokerError | LLMError | CircuitBreakerOpenError):
        reason = str(outcome)
    elif isinstance(outcome, BaseException):
        logger.exception("On-demand %s analysis failed for %s", agent_type.value, stock.symbol, exc_info=outcome)
        reason = f"{type(outcome).__name__}: {outcome}"
    else:  # pragma: no cover - gather only ever returns results or exceptions
        return None

    messages.append(f"{agent_type.value.title()} Agent: {reason}")
    return None


def _first_message(messages: list[str]) -> str | None:
    return messages[0] if messages else None
