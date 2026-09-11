from __future__ import annotations

from sqlalchemy import select

from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings
from vibetrading.core.models import Stock
from vibetrading.llm.providers.mock_provider import MockLLMAdapter
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.orchestrator.scheduler import OrchestratorScheduler
from vibetrading.persistence.orm_models import AgentRunORM, AuditLogORM, OrderORM, SignalORM


async def test_paper_mode_accelerated_cycles_accumulate_rows_and_publish_events(db_session):
    """Drives the real orchestration pipeline (Research -> Technical ->
    Strategy -> Risk -> Execution) through several accelerated cycles in
    PAPER mode, without waiting on real APScheduler intervals -- exactly the
    Phase 7 DoD: signals/orders/audit rows accumulate and the WS event bus
    fires for each stage.
    """
    settings = Settings(_env_file=None, risk_min_signal_confidence=0.1)
    broker = MockBrokerClient(seed=1)
    scheduler = OrchestratorScheduler(broker=broker, tenant_id=1, watchlist=[Stock(symbol="TCS")], settings=settings)

    # Force a deterministic BUY every cycle so the pipeline actually reaches
    # the Risk Agent and places real (paper) orders, independent of the
    # specific technical/research content generated this run.
    scripted_strategy_agent = StrategyAgent(
        llm=MockLLMAdapter(
            default_response='{"action": "buy", "confidence": 0.9, "reasoning": "test buy", "stop_loss_pct": 0.03}'
        )
    )
    scheduler.strategy_agent = scripted_strategy_agent
    scheduler.pipeline.strategy_agent = scripted_strategy_agent

    stock = Stock(symbol="TCS")

    queue = event_bus.subscribe(1)
    try:
        for _ in range(3):
            await scheduler.pipeline.run_research_cycle(db_session, stock)
            await scheduler.pipeline.run_technical_cycle(db_session, stock)
            await scheduler.pipeline.run_strategy_cycle(db_session, stock)

        agent_runs = (await db_session.execute(select(AgentRunORM))).scalars().all()
        signals = (await db_session.execute(select(SignalORM))).scalars().all()
        audit_entries = (await db_session.execute(select(AuditLogORM))).scalars().all()
        orders = (await db_session.execute(select(OrderORM))).scalars().all()

        assert len(agent_runs) == 6  # 3 research + 3 technical
        assert {r.agent_type for r in agent_runs} == {"research", "technical"}
        assert len(signals) == 3
        assert all(s.action == "buy" for s in signals)
        assert len(audit_entries) == 3
        assert len(orders) >= 1  # risk gating may cap later buys as funds/exposure shrink

        messages = []
        while not queue.empty():
            messages.append(queue.get_nowait())

        message_types = {m["type"] for m in messages}
        assert "agent_output" in message_types
        assert "signal" in message_types
        assert "risk_decision" in message_types
        assert "order" in message_types
    finally:
        event_bus.unsubscribe(1, queue)


async def test_paper_mode_strategy_cycle_skips_when_no_technical_output_yet(db_session):
    settings = Settings(_env_file=None)
    broker = MockBrokerClient(seed=2)
    scheduler = OrchestratorScheduler(broker=broker, tenant_id=1, watchlist=[Stock(symbol="INFY")], settings=settings)
    stock = Stock(symbol="INFY")

    # No research/technical cycle has run yet for this stock.
    result = await scheduler.pipeline.run_strategy_cycle(db_session, stock)

    assert result is None
    signals = (await db_session.execute(select(SignalORM))).scalars().all()
    assert signals == []
