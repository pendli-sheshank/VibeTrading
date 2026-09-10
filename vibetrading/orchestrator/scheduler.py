from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.agents.strategy.technical_agent import TechnicalAgent
from vibetrading.broker.base import BrokerClient
from vibetrading.config import Settings
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.llm.router import LLMRouter
from vibetrading.orchestrator.pipeline import TradingPipeline
from vibetrading.orchestrator.stop_loss_monitor import StopLossMonitor
from vibetrading.orchestrator.watchlist import get_watchlist
from vibetrading.persistence.db import get_session
from vibetrading.risk.engine import RiskEngine
from vibetrading.settings.cache import get_tenant_settings

logger = logging.getLogger(__name__)


class OrchestratorScheduler:
    """The AI Orchestrator: wires up Research/Technical/Strategy/Risk into a
    TradingPipeline and schedules it per stock via APScheduler
    (AsyncIOScheduler, in-process — no Celery/Kafka needed at this scale,
    since this is a single deployable service). Each job opens its own DB
    session per tick; sessions are never shared across concurrent job runs.

    `watchlist` is resolved by the caller (see build_scheduler() below) and
    passed in explicitly — the DB-backed watchlist can only be read async,
    so this constructor stays synchronous and doesn't resolve it itself.
    """

    def __init__(
        self,
        broker: BrokerClient,
        tenant_id: int,
        watchlist: list[Stock],
        settings: Settings | None = None,
        fencing_token: int | None = None,
    ):
        self.broker = broker
        self.tenant_id = tenant_id
        self.settings = settings or get_tenant_settings(tenant_id)
        self.watchlist: list[Stock] = watchlist

        llm_router = LLMRouter(self.settings)
        self.research_agent = ResearchAgent(llm=llm_router.get_adapter(AgentType.RESEARCH), settings=self.settings)
        self.technical_agent = TechnicalAgent(broker=broker)
        self.strategy_agent = StrategyAgent(llm=llm_router.get_adapter(AgentType.STRATEGY))
        # fencing_token: this process's proof of exclusive ownership of
        # this tenant's trading loop, as of the last successful lease
        # acquisition/renewal (see orchestrator/lease.py and
        # orchestrator/manager.py's renewal loop). None means no
        # distributed enforcement -- single-process dev/test, matching
        # this class's pre-Phase-20 behavior.
        self.risk_engine = RiskEngine(broker=broker, tenant_id=tenant_id, settings=self.settings, fencing_token=fencing_token)
        self.pipeline = TradingPipeline(
            tenant_id=tenant_id,
            research_agent=self.research_agent,
            technical_agent=self.technical_agent,
            strategy_agent=self.strategy_agent,
            risk_engine=self.risk_engine,
        )
        self.stop_loss_monitor = StopLossMonitor(broker=broker, risk_engine=self.risk_engine)

        self.scheduler = AsyncIOScheduler()

        # APScheduler's AsyncIOExecutor.shutdown() does NOT honor wait=True —
        # by its own source comment, "there is no way to honor wait=True
        # without converting this method into a coroutine method," so it
        # just cancels every in-flight job task regardless of the wait
        # argument. That's the opposite of what a restart needs (never
        # discard an order placement mid-flight), so we track in-flight
        # jobs ourselves and drain them before ever calling
        # self.scheduler.shutdown() — see shutdown_gracefully().
        self._active_jobs = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    def _job_started(self) -> None:
        self._active_jobs += 1
        self._idle_event.clear()

    def _job_finished(self) -> None:
        self._active_jobs = max(0, self._active_jobs - 1)
        if self._active_jobs == 0:
            self._idle_event.set()

    async def wait_until_idle(self) -> None:
        await self._idle_event.wait()

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
        """Immediate, non-graceful stop — may cancel an in-flight job (see
        the AsyncIOExecutor note in __init__). Safe to call once nothing is
        running (e.g. after shutdown_gracefully() has already drained
        active jobs); prefer shutdown_gracefully() wherever a job could
        plausibly be mid-flight, such as an orchestrator restart."""
        self.scheduler.shutdown(wait=False)

    async def shutdown_gracefully(self) -> None:
        """Stops new jobs from starting, waits for any currently-running
        job to finish naturally, then tears down the scheduler — the safe
        way to discard this scheduler without risking a cancelled order
        placement mid-flight."""
        if self.scheduler.running:
            self.scheduler.pause()
        await self.wait_until_idle()
        self.shutdown()

    async def run_research_job(self, stock: Stock) -> None:
        self._job_started()
        try:
            async with get_session() as session:
                await self.pipeline.run_research_cycle(session, stock)
        except Exception:
            logger.exception("Research cycle failed for %s", stock.symbol)
        finally:
            self._job_finished()

    async def run_strategy_job(self, stock: Stock) -> None:
        self._job_started()
        try:
            async with get_session() as session:
                await self.pipeline.run_technical_cycle(session, stock)
                await self.pipeline.run_strategy_cycle(session, stock)
        except Exception:
            logger.exception("Strategy cycle failed for %s", stock.symbol)
        finally:
            self._job_finished()

    async def run_stop_loss_monitor_job(self) -> None:
        self._job_started()
        try:
            async with get_session() as session:
                await self.stop_loss_monitor.check_all(session)
        except Exception:
            logger.exception("Stop-loss monitor cycle failed")
        finally:
            self._job_finished()


async def build_scheduler(
    broker: BrokerClient, tenant_id: int, settings: Settings | None = None, fencing_token: int | None = None
) -> OrchestratorScheduler:
    """Resolves the tenant's DB-backed watchlist and constructs an
    OrchestratorScheduler — the async counterpart to the (synchronous)
    constructor, used by orchestrator/runtime.py wherever a scheduler needs
    to be (re)built."""
    async with get_session() as session:
        watchlist = await get_watchlist(session, tenant_id)
    return OrchestratorScheduler(
        broker=broker, tenant_id=tenant_id, watchlist=watchlist, settings=settings, fencing_token=fencing_token
    )
