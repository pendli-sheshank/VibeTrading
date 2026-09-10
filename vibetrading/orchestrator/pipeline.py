from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.agents.strategy.technical_agent import TechnicalAgent
from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.repositories import (
    agent_output_from_orm,
    get_latest_agent_output,
    save_agent_output,
    save_signal,
)
from vibetrading.risk.engine import ExecutionResult, RiskEngine

logger = logging.getLogger(__name__)

# How stale a persisted AgentOutput can be before the Strategy cycle treats
# it as unavailable rather than synthesizing against outdated data.
DEFAULT_FRESHNESS_WINDOW = timedelta(hours=24)


class TradingPipeline:
    """The live-trading orchestration glue: Research/Technical agent runs ->
    Strategy synthesis -> Risk Agent gate -> Execution -> audit log ->
    Monitoring push (event_bus). This is what orchestrator/scheduler.py
    calls on each job tick for each stock.
    """

    def __init__(
        self,
        tenant_id: int,
        research_agent: ResearchAgent,
        technical_agent: TechnicalAgent,
        strategy_agent: StrategyAgent,
        risk_engine: RiskEngine,
        freshness_window: timedelta = DEFAULT_FRESHNESS_WINDOW,
    ):
        self.tenant_id = tenant_id
        self.research_agent = research_agent
        self.technical_agent = technical_agent
        self.strategy_agent = strategy_agent
        self.risk_engine = risk_engine
        self.freshness_window = freshness_window

    async def run_research_cycle(self, session: AsyncSession, stock: Stock) -> AgentOutput:
        output = await self.research_agent.analyze(stock, context={})
        await save_agent_output(session, self.tenant_id, output)
        await session.commit()
        await event_bus.publish(
            {
                "type": "agent_output",
                "agent_type": AgentType.RESEARCH.value,
                "stock_symbol": stock.symbol,
                "confidence": output.confidence,
            }
        )
        return output

    async def run_technical_cycle(self, session: AsyncSession, stock: Stock) -> AgentOutput:
        output = await self.technical_agent.analyze(stock, context={})
        await save_agent_output(session, self.tenant_id, output)
        await session.commit()
        await event_bus.publish(
            {
                "type": "agent_output",
                "agent_type": AgentType.TECHNICAL.value,
                "stock_symbol": stock.symbol,
                "confidence": output.confidence,
            }
        )
        return output

    async def _fresh_latest_output(
        self, session: AsyncSession, stock: Stock, agent_type: AgentType
    ) -> AgentOutput | None:
        orm = await get_latest_agent_output(session, self.tenant_id, stock.symbol, agent_type.value)
        if orm is None:
            return None
        age = datetime.now(UTC) - orm.timestamp.replace(tzinfo=UTC)
        if age > self.freshness_window:
            logger.info("Stale %s output for %s (age %s); treating as unavailable.", agent_type.value, stock.symbol, age)
            return None
        return agent_output_from_orm(orm)

    async def run_strategy_cycle(self, session: AsyncSession, stock: Stock) -> ExecutionResult | None:
        """Synthesizes a Signal from the latest fresh Technical/Research
        outputs and runs it through the Risk Agent. Returns None if there's
        no fresh Technical read yet (nothing to synthesize against).
        """
        technical_output = await self._fresh_latest_output(session, stock, AgentType.TECHNICAL)
        if technical_output is None:
            logger.info("No fresh technical output for %s yet; skipping strategy cycle.", stock.symbol)
            return None

        research_output = await self._fresh_latest_output(session, stock, AgentType.RESEARCH)
        outputs = [technical_output] + ([research_output] if research_output else [])

        signal = await self.strategy_agent.synthesize(stock, outputs)
        signal_orm = await save_signal(session, self.tenant_id, signal)
        await session.flush()

        await event_bus.publish(
            {
                "type": "signal",
                "stock_symbol": stock.symbol,
                "action": signal.action.value,
                "confidence": signal.confidence,
            }
        )

        result = await self.risk_engine.approve_and_execute(session, signal, stock, signal_id=signal_orm.id)
        await session.commit()

        await event_bus.publish(
            {
                "type": "risk_decision",
                "stock_symbol": stock.symbol,
                "approved": result.approved,
                "reasons": result.risk_check.reasons,
            }
        )
        if result.order_result is not None:
            await event_bus.publish(
                {
                    "type": "order",
                    "stock_symbol": stock.symbol,
                    "order_id": result.order_result.order_id,
                    "status": result.order_result.status.value,
                }
            )

        return result
