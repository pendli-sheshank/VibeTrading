from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.agents.strategy.technical_agent import TechnicalAgent
from vibetrading.broker.base import BrokerClient
from vibetrading.config import Settings, get_settings
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.llm.router import LLMRouter
from vibetrading.orchestrator.pipeline import TradingPipeline
from vibetrading.orchestrator.stop_loss_monitor import StopLossMonitor
from vibetrading.orchestrator.watchlist import get_watchlist
from vibetrading.persistence.db import get_session
from vibetrading.risk.engine import RiskEngine

logger = logging.getLogger(__name__)


class OrchestratorScheduler:
    """The AI Orchestrator: wires up Research/Technical/Strategy/Risk into a
    TradingPipeline and schedules it per stock via APScheduler
    (AsyncIOScheduler, in-process — no Celery/Kafka needed at this scale,
    since this is a single deployable service). Each job opens its own DB
    session per tick; sessions are never shared across concurrent job runs.
    """

    def __init__(self, broker: BrokerClient, settings: Settings | None = None):
        self.broker = broker
        self.settings = settings or get_settings()
        self.watchlist: list[Stock] = get_watchlist(self.settings)

        llm_router = LLMRouter(self.settings)
        self.research_agent = ResearchAgent(llm=llm_router.get_adapter(AgentType.RESEARCH))
        self.technical_agent = TechnicalAgent(broker=broker)
        self.strategy_agent = StrategyAgent(llm=llm_router.get_adapter(AgentType.STRATEGY))
        self.risk_engine = RiskEngine(broker=broker, settings=self.settings)
        self.pipeline = TradingPipeline(
            research_agent=self.research_agent,
            technical_agent=self.technical_agent,
            strategy_agent=self.strategy_agent,
            risk_engine=self.risk_engine,
        )
        self.stop_loss_monitor = StopLossMonitor(broker=broker, risk_engine=self.risk_engine)

        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        now = datetime.now(UTC)
        for stock in self.watchlist:
            self.scheduler.add_job(
                self.run_research_job,
                "interval",
                seconds=self.settings.agent_interval_research_sec,
                args=[stock],
                id=f"research-{stock.symbol}",
                max_instances=1,
                coalesce=True,
                next_run_time=now,
            )
            self.scheduler.add_job(
                self.run_strategy_job,
                "interval",
                seconds=self.settings.agent_interval_strategy_sec,
                args=[stock],
                id=f"strategy-{stock.symbol}",
                max_instances=1,
                coalesce=True,
                next_run_time=now,
            )

        self.scheduler.add_job(
            self.run_stop_loss_monitor_job,
            "interval",
            seconds=self.settings.agent_interval_stop_loss_monitor_sec,
            id="stop-loss-monitor",
            max_instances=1,
            coalesce=True,
            next_run_time=now,
        )

        self.scheduler.start()
        logger.info("Orchestrator scheduler started for %d stock(s).", len(self.watchlist))

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    async def run_research_job(self, stock: Stock) -> None:
        try:
            async with get_session() as session:
                await self.pipeline.run_research_cycle(session, stock)
        except Exception:
            logger.exception("Research cycle failed for %s", stock.symbol)

    async def run_strategy_job(self, stock: Stock) -> None:
        try:
            async with get_session() as session:
                await self.pipeline.run_technical_cycle(session, stock)
                await self.pipeline.run_strategy_cycle(session, stock)
        except Exception:
            logger.exception("Strategy cycle failed for %s", stock.symbol)

    async def run_stop_loss_monitor_job(self) -> None:
        try:
            async with get_session() as session:
                await self.stop_loss_monitor.check_all(session)
        except Exception:
            logger.exception("Stop-loss monitor cycle failed")
